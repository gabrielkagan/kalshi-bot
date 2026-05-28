"""B2b-1 — evaluated_opportunities synthetic-RTI shadow columns + the
load-bearing zero-live-decision-change invariant (persistence side).

Ticket 86ba64h2w (program 86ba64gyq). Plan: kb/decisions/b2b-1-core-shadow-plan.md.

TDD-first: these fail until bot/state.py adds the three columns
(``rti_synthetic`` / ``rti_constituent_count`` / ``rti_confidence``), the
``_scan_rti_cache`` per-asset cache, and the
``insert_evaluated_opportunity`` auto-fill (mirroring the
``_scan_cx_gap_cache`` → ``spot_coinbase_kraken_gap_bps`` precedent).

The synthetic is SHADOW-only: it is WRITE-ONLY into evaluated_opportunities
and is never read by any decision path. The byte-identical test proves the
cache auto-fill touches ONLY the three rti columns — every other persisted
field (spot, edge, calibrated_prob, sizing, ...) is identical whether the
cache is populated or empty.
"""
from __future__ import annotations

import sqlite3

import pytest

from bot.state import StateManager


RTI_COLUMNS = ("rti_synthetic", "rti_constituent_count", "rti_confidence")


@pytest.fixture()
def state(tmp_path):
    sm = StateManager(db_path=str(tmp_path / "state.db"))
    yield sm
    try:
        sm.conn.close()
    except Exception:
        pass


def _columns(state) -> set:
    rows = state.conn.execute("PRAGMA table_info(evaluated_opportunities)").fetchall()
    return {r[1] for r in rows}


def test_rti_columns_exist(state):
    cols = _columns(state)
    for c in RTI_COLUMNS:
        assert c in cols, f"missing evaluated_opportunities.{c}"


def test_rti_column_affinities(state):
    info = {r[1]: r[2].upper() for r in
            state.conn.execute("PRAGMA table_info(evaluated_opportunities)").fetchall()}
    assert info["rti_synthetic"] == "REAL"
    assert info["rti_constituent_count"] == "INTEGER"
    assert info["rti_confidence"] == "REAL"


def test_insert_auto_fills_rti_from_cache(state):
    """A populated _scan_rti_cache[asset] auto-fills the three columns on a
    candidate-path insert — no per-call threading (mirrors cx_gap)."""
    state._scan_rti_cache["BTC"] = (100.05, 3, 0.75)
    state.insert_evaluated_opportunity(
        "KXBTC-T1", "KXBTC", "BTC", "candidate",
        spot_price=100.04, edge=0.05, calibrated_prob=0.92,
    )
    row = state.conn.execute(
        "SELECT rti_synthetic, rti_constituent_count, rti_confidence "
        "FROM evaluated_opportunities WHERE ticker='KXBTC-T1'"
    ).fetchone()
    assert row[0] == pytest.approx(100.05)
    assert row[1] == 3
    assert row[2] == pytest.approx(0.75)


def test_insert_rti_null_when_cache_empty(state):
    state.insert_evaluated_opportunity(
        "KXBTC-T2", "KXBTC", "BTC", "candidate",
        spot_price=100.04, edge=0.05, calibrated_prob=0.92,
    )
    row = state.conn.execute(
        "SELECT rti_synthetic, rti_constituent_count, rti_confidence "
        "FROM evaluated_opportunities WHERE ticker='KXBTC-T2'"
    ).fetchone()
    assert tuple(row) == (None, None, None)


def test_explicit_kwarg_wins_over_cache(state):
    state._scan_rti_cache["BTC"] = (100.05, 3, 0.75)
    state.insert_evaluated_opportunity(
        "KXBTC-T3", "KXBTC", "BTC", "candidate",
        spot_price=100.04, edge=0.05, calibrated_prob=0.92,
        rti_synthetic=200.0, rti_constituent_count=1, rti_confidence=0.25,
    )
    row = state.conn.execute(
        "SELECT rti_synthetic, rti_constituent_count, rti_confidence "
        "FROM evaluated_opportunities WHERE ticker='KXBTC-T3'"
    ).fetchone()
    assert tuple(row) == (200.0, 1, 0.25)


def test_byte_identical_except_rti_columns(state):
    """Load-bearing zero-live-decision-change proof (persistence side):
    inserting with the rti cache populated vs empty produces rows that are
    IDENTICAL in every column EXCEPT the three rti columns. The synthetic
    cannot leak into spot / edge / calibrated_prob / sizing / any other
    persisted decision field."""
    common = dict(
        event_ticker="KXBTC", asset="BTC", filter_stage="candidate",
        spot_price=100.04, threshold=100.0, volatility=0.4, market_price=92,
        seconds_to_close=300.0, calibrated_prob=0.92, edge=0.05,
        strategy="threshold_momentum", position_size=10, kelly_f=0.03,
        side="yes",
    )
    # Row A: cache empty.
    state._scan_rti_cache.clear()
    state.insert_evaluated_opportunity("KXBTC-OFF", **common)
    # Row B: cache populated.
    state._scan_rti_cache["BTC"] = (100.05, 4, 1.0)
    state.insert_evaluated_opportunity("KXBTC-ON", **common)

    cols = [r[1] for r in
            state.conn.execute("PRAGMA table_info(evaluated_opportunities)").fetchall()]
    # Exclude the row identity + per-insert timestamp (evaluation_time differs
    # by microseconds between the two inserts — not a decision field).
    keep = [c for c in cols if c not in RTI_COLUMNS
            and c not in ("id", "ticker", "created_at", "evaluated_at",
                          "evaluation_time", "timestamp")]
    sel = ", ".join(keep)
    a = state.conn.execute(
        f"SELECT {sel} FROM evaluated_opportunities WHERE ticker='KXBTC-OFF'").fetchone()
    b = state.conn.execute(
        f"SELECT {sel} FROM evaluated_opportunities WHERE ticker='KXBTC-ON'").fetchone()
    assert a == b, "rti cache leaked into a non-rti column (decision field changed)"

    # And the rti columns DID change (sanity: the test actually exercised the path).
    off = state.conn.execute(
        "SELECT rti_synthetic FROM evaluated_opportunities WHERE ticker='KXBTC-OFF'").fetchone()
    on = state.conn.execute(
        "SELECT rti_synthetic FROM evaluated_opportunities WHERE ticker='KXBTC-ON'").fetchone()
    assert off[0] is None and on[0] == pytest.approx(100.05)
