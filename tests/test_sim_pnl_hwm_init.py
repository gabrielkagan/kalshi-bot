"""TDD for sim_pnl HWM init bug — H1 from kb/findings/sim-pnl-live-ws-divergence-rca-may05.md.

Bug: reconstruct_hwm_init returns SELECT MAX(available_balance_cents)
over all history. Bot.py uses a 7-day rolling HWM. When the bot is in
deep drawdown (recent balance << historical peak), sim_pnl thinks
balance == HWM == no drawdown, drawdown scaler doesn't fire, sizing
is at top tier (25% Kelly). Empirically: production at 1% Kelly,
sim_pnl at 25% Kelly → 25× sizing divergence → all PnL anomalies
downstream.

Fix: reconstruct_hwm_init should use the MAX over the 7 days BEFORE
test_start_ts, not all-time max. Matches bot/_impl.py rolling-HWM behavior.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

# CI runner doesn't install pandas/torch (heavy deps not in
# requirements.txt). importorskip skips the whole file at collection
# time when the dep is missing rather than crashing pytest. Sibling
# test files (test_sim_pnl_gate_prob_source.py etc.) already follow
# this pattern.
#
# Both pandas AND torch must skip-on-missing — Pillar 3 added
# pandas to [dev], so the pandas skip alone no longer covers CI:
# scripts/cal_mlp/* (imported via REPO sys.path below) imports torch,
# which is in [ml] only; without torch, this file's tests now collect
# successfully (pandas present) and crash at fixture time on the torch
# import. Skip on either missing dep restores pre-Pillar-3 behavior.
pd = pytest.importorskip("pandas")
pytest.importorskip("torch")

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts" / "cal_mlp"))


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS evaluated_opportunities (
    id INTEGER PRIMARY KEY,
    ticker TEXT NOT NULL,
    event_ticker TEXT NOT NULL,
    asset TEXT NOT NULL,
    filter_stage TEXT NOT NULL,
    evaluation_time TEXT NOT NULL,
    available_balance_cents INTEGER,
    status TEXT NOT NULL DEFAULT 'pending',
    product_type TEXT
);
"""


def _seed_db(rows: list[dict]) -> str:
    """Create a tempfile sqlite DB seeded with `rows` and return its path.

    WAL mode is set BEFORE seeding so the ro-handle in
    reconstruct_hwm_init doesn't try to write pragma at runtime.
    Mirrors how the production state.db snapshot already has WAL set.
    """
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA_SQL)
    for r in rows:
        conn.execute(
            """INSERT INTO evaluated_opportunities
               (ticker, event_ticker, asset, filter_stage, evaluation_time,
                available_balance_cents, product_type)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (r['ticker'], r['event_ticker'], r['asset'], r.get('filter_stage', 'candidate'),
             r['evaluation_time'], r.get('available_balance_cents'),
             r.get('product_type', '15m')),
        )
    conn.commit()
    conn.close()
    return path


def test_reconstruct_hwm_returns_7d_rolling_max_not_all_time_max():
    """The leading bug. Bot.py uses 7d rolling HWM; sim_pnl currently
    uses all-time max. With deep drawdown the difference is huge.

    Setup:
      - 8 days ago: balance = $20.00 (OLD PEAK, outside 7-day window)
      - 3 days ago: balance = $7.00 (within 7-day window)
      - 1 day  ago: balance = $5.00 (within 7-day window)
      - test_start = now

    Expected: HWM = $7.00 (7-day rolling max).
    Current behavior:  HWM = $20.00 (all-time max). FAILS until fix.
    """
    from sim_pnl import reconstruct_hwm_init

    now = pd.Timestamp.now(tz='UTC').replace(microsecond=0)
    rows = [
        # 8 days ago — historical peak, OUTSIDE 7-day window
        {'ticker': 'OLD', 'event_ticker': 'KX', 'asset': 'BTC',
         'evaluation_time': (now - pd.Timedelta(days=8)).isoformat().replace('+00:00', 'Z'),
         'available_balance_cents': 2000},
        # 3 days ago — within 7-day window (this is the recent peak)
        {'ticker': 'MID', 'event_ticker': 'KX', 'asset': 'BTC',
         'evaluation_time': (now - pd.Timedelta(days=3)).isoformat().replace('+00:00', 'Z'),
         'available_balance_cents': 700},
        # 1 day ago — within 7-day window (latest balance, in drawdown)
        {'ticker': 'NEW', 'event_ticker': 'KX', 'asset': 'BTC',
         'evaluation_time': (now - pd.Timedelta(days=1)).isoformat().replace('+00:00', 'Z'),
         'available_balance_cents': 500},
    ]
    db_path = _seed_db(rows)
    try:
        hwm, _start, source = reconstruct_hwm_init(db_path, now)
        assert hwm == 700, (
            f"7d rolling HWM should be 700 (the recent peak within the "
            f"7-day window before test_start). Got {hwm}. "
            f"If hwm == 2000, sim_pnl is using all-time max — see "
            f"kb/findings/sim-pnl-live-ws-divergence-rca-may05.md H1."
        )
        assert source == 'balance_walked'
    finally:
        os.unlink(db_path)


def test_reconstruct_hwm_excludes_balance_after_test_start():
    """test_start cutoff must exclude future-relative-to-window rows.

    Setup:
      - 5 days ago: $5.00 (within 7-day window, before test_start)
      - now (test_start): not relevant
      - 1 day after test_start: $99.99 (must be excluded)

    Expected: HWM = $5.00, NOT $99.99.
    """
    from sim_pnl import reconstruct_hwm_init

    test_start = pd.Timestamp.now(tz='UTC').replace(microsecond=0)
    rows = [
        {'ticker': 'A', 'event_ticker': 'KX', 'asset': 'BTC',
         'evaluation_time': (test_start - pd.Timedelta(days=5)).isoformat().replace('+00:00', 'Z'),
         'available_balance_cents': 500},
        {'ticker': 'B', 'event_ticker': 'KX', 'asset': 'BTC',
         'evaluation_time': (test_start + pd.Timedelta(days=1)).isoformat().replace('+00:00', 'Z'),
         'available_balance_cents': 9999},
    ]
    db_path = _seed_db(rows)
    try:
        hwm, _start, source = reconstruct_hwm_init(db_path, test_start)
        assert hwm == 500, (
            f"HWM should ignore rows after test_start. Got {hwm} "
            f"(if 9999, the test_start cutoff is broken)."
        )
    finally:
        os.unlink(db_path)


def test_reconstruct_hwm_falls_back_to_forward_only_when_no_history():
    """Empty pre-window history → fall back to current balance from latest row."""
    from sim_pnl import reconstruct_hwm_init

    test_start = pd.Timestamp.now(tz='UTC').replace(microsecond=0)
    rows = [
        # All rows are AFTER test_start. Pre-window history is empty.
        {'ticker': 'A', 'event_ticker': 'KX', 'asset': 'BTC',
         'evaluation_time': (test_start + pd.Timedelta(hours=1)).isoformat().replace('+00:00', 'Z'),
         'available_balance_cents': 1234},
    ]
    db_path = _seed_db(rows)
    try:
        hwm, _start, source = reconstruct_hwm_init(db_path, test_start)
        assert hwm == 1234, f"Should fall back to latest available_balance_cents. Got {hwm}"
        assert source == 'forward_only_from_now'
    finally:
        os.unlink(db_path)


def test_reconstruct_hwm_with_only_old_history_fails_appropriately():
    """All-history-pre-7d-window → should still return a sensible HWM
    (either fall back to latest or use the older window if no recent data).

    Setup: ONLY a 30-day-old row, $1.00. Within 7-day window of
    test_start there are no rows.

    Expected: We accept either (a) returning that $1.00 with
    'balance_walked' source (used 30-day old data because no 7-day data
    exists) OR (b) returning 0 with 'forward_only_from_now'. Both are
    defensible. What we MUST NOT do: return a hard-coded "default"
    that masks the missing data.
    """
    from sim_pnl import reconstruct_hwm_init

    test_start = pd.Timestamp.now(tz='UTC').replace(microsecond=0)
    rows = [
        {'ticker': 'OLD', 'event_ticker': 'KX', 'asset': 'BTC',
         'evaluation_time': (test_start - pd.Timedelta(days=30)).isoformat().replace('+00:00', 'Z'),
         'available_balance_cents': 100},
    ]
    db_path = _seed_db(rows)
    try:
        hwm, _start, source = reconstruct_hwm_init(db_path, test_start)
        # Either fallback path is acceptable; assert we don't return a
        # nonsense value.
        assert hwm in (100, 0), f"unexpected hwm={hwm}"
        assert source in ('balance_walked', 'forward_only_from_now')
    finally:
        os.unlink(db_path)


def test_reconstruct_hwm_returns_separate_start_balance():
    """H1b: reconstruct_hwm_init must also return the actual balance AT
    the start of the window (NOT the HWM). The drawdown scaler ratio is
    current_balance / hwm — when bot is in drawdown, these differ.

    Setup: bot was at $10.00 5 days ago (recent peak), now at $5.00 (drawdown
    50% from recent peak, well into halt-floor territory).

    Expected: returns (hwm=$10.00, start_balance=$5.00, source).
    Pre-fix: returns only hwm; sim_pnl initializes current_balance = hwm
    so drawdown ratio = 1.0 → no drawdown scaler → top-tier sizing.
    """
    from sim_pnl import reconstruct_hwm_init

    test_start = pd.Timestamp.now(tz='UTC').replace(microsecond=0)
    rows = [
        # 5 days ago: peak balance
        {'ticker': 'A', 'event_ticker': 'KX', 'asset': 'BTC',
         'evaluation_time': (test_start - pd.Timedelta(days=5)).isoformat().replace('+00:00', 'Z'),
         'available_balance_cents': 1000},
        # 1 day ago: in drawdown (latest)
        {'ticker': 'B', 'event_ticker': 'KX', 'asset': 'BTC',
         'evaluation_time': (test_start - pd.Timedelta(days=1)).isoformat().replace('+00:00', 'Z'),
         'available_balance_cents': 500},
    ]
    db_path = _seed_db(rows)
    try:
        ret = reconstruct_hwm_init(db_path, test_start)
        # Post-fix: returns 3-tuple (hwm, start_balance, source).
        # Pre-fix returns 2-tuple (hwm, source) — this test must FAIL until fix.
        assert len(ret) == 3, (
            f"reconstruct_hwm_init must return (hwm, start_balance, source). "
            f"Got {len(ret)}-tuple. See H1b in "
            f"kb/findings/sim-pnl-live-ws-divergence-rca-may05.md."
        )
        hwm, start_balance, source = ret
        assert hwm == 1000, f"7d HWM should be 1000, got {hwm}"
        assert start_balance == 500, (
            f"start_balance should be the LATEST pre-window balance ($5.00 = 500c), "
            f"got {start_balance}. If equal to hwm (1000), the bug is unfixed."
        )
    finally:
        os.unlink(db_path)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
