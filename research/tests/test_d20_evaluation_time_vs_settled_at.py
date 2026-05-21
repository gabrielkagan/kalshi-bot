"""D-20 — evaluation_time vs settled_at are different timestamps.

Authoritative source: bot._impl::insert_evaluated_opportunity sets evaluation_time
at decision moment; mark_evaluated_opportunity_settled sets settled_at later
(per RCA D-20). These differ:
- 15m: 0.5–15 min
- hourly: hours
- weather: 24h+

Replay's regime_cutoff (D-9) and lookback windows must clamp on the right
column per query:
- "What was the decision regime?" → evaluation_time >= cutoff
- "What was the realized regime?" → settled_at >= cutoff

For cf aggregation, USE evaluation_time (cf is computed at decision config + actual
result, not at re-decision under post-cutoff config).

Most assertions here are TDD-red until B3 ships evaluate_window — they pin
which timestamp column is consulted.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
import textwrap
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def cross_regime_snapshot(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Snapshot with rows that have evaluation_time pre-cutoff but settled_at post-cutoff.

    Simulates a weather decision made before the regime cutoff that settled
    days later (after the cutoff). Replay must clamp on evaluation_time (the
    decision regime), not settled_at.
    """
    db = tmp_path_factory.mktemp("d20") / "cross_regime.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.executescript(textwrap.dedent("""
            CREATE TABLE evaluated_opportunities (
                id INTEGER PRIMARY KEY,
                evaluation_time TEXT NOT NULL,
                settled_at TEXT,
                market_result TEXT,
                side TEXT DEFAULT 'yes',
                market_price INTEGER,
                position_size INTEGER,
                product_type TEXT,
                filter_stage TEXT DEFAULT 'candidate',
                status TEXT DEFAULT 'settled',
                counterfactual_pnl INTEGER
            );
        """))
        conn.executemany(
            """INSERT INTO evaluated_opportunities
               (evaluation_time, settled_at, market_result, market_price, position_size,
                product_type, filter_stage, status, counterfactual_pnl)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                # Pre-cutoff decision, post-cutoff settlement (weather pattern):
                # evaluation_time=2026-04-30T10:00 (pre 16:16), settled_at=2026-05-01T10:00 (post)
                ("2026-04-30T10:00:00.000Z", "2026-05-01T10:00:00.000Z", "yes", 50, 1, "weather", "candidate", "settled", 48),
                # Pre-cutoff decision, pre-cutoff settlement (15m baseline)
                ("2026-04-30T10:00:00.000Z", "2026-04-30T10:15:00.000Z", "yes", 50, 1, "15m", "candidate", "settled", 48),
                # Post-cutoff decision, post-cutoff settlement (15m post-regime)
                ("2026-05-01T10:00:00.000Z", "2026-05-01T10:15:00.000Z", "yes", 50, 1, "15m", "candidate", "settled", 48),
            ],
        )
        conn.commit()
    finally:
        conn.close()
    return db


def test_d20_cf_aggregation_uses_evaluation_time(cross_regime_snapshot: Path) -> None:
    """Replay's cf aggregation clamps on evaluation_time (decision regime), not settled_at.

    With regime_cutoff=2026-04-30T16:16:00:
    - The cross-regime row (decision 04-30 pre, settle 05-01 post) is EXCLUDED
      (decision was pre-cutoff).
    - Pure pre-cutoff and pure post-cutoff rows are filtered as expected.

    If replay incorrectly clamps on settled_at, the cross-regime row would be
    INCLUDED, polluting the post-cutoff aggregate.
    """
    import research.replay as rep
    if not hasattr(rep, "evaluate_window"):
        pytest.skip("D-20 TDD-red: evaluate_window not yet implemented")
    cutoff = dt.datetime(2026, 4, 30, 16, 16, 0, tzinfo=dt.timezone.utc)
    result = rep.evaluate_window(
        snapshot_path=cross_regime_snapshot,
        regime_cutoff=cutoff,
    )
    # Expect 1 row (the post-cutoff 15m). Pre-cutoff 15m and cross-regime weather
    # are both excluded based on their evaluation_time.
    assert getattr(result, "row_count", None) == 1, (
        f"D-20 cf aggregation: expected 1 post-cutoff row by evaluation_time, "
        f"got {result!r}"
    )


def test_d20_replay_does_not_aggregate_on_settled_at_silently() -> None:
    """AST guard: replay.py SQL aggregations must reference `evaluation_time`, not `settled_at`.

    This is a heuristic — if B3 adds a query that does
        `WHERE settled_at >= cutoff` AS THE PRIMARY CUTOFF FILTER,
    that's likely a bug. The legitimate case for settled_at is realized-regime
    diagnostics, not cf aggregation.
    """
    import inspect
    import research.replay as rep
    src = inspect.getsource(rep)
    # Pin that any settled_at filter is gated by an explicit comment
    # acknowledging the realized-vs-decision semantic.
    if "settled_at" in src:
        # If settled_at appears, require a nearby pin comment or a
        # `# D-20 realized-regime opt-in` comment.
        assert "D-20" in src or "realized-regime" in src or "realized regime" in src, (
            "D-20 settled_at usage in replay.py: must be accompanied by a "
            "comment acknowledging the realized-vs-decision regime semantic."
        )


def test_d20_settled_at_can_be_null_evaluation_time_cannot(cross_regime_snapshot: Path) -> None:
    """evaluation_time is NOT NULL (decision moment is always known).
    settled_at CAN be NULL (row not yet settled).

    Per the snapshot DDL: `evaluation_time TEXT NOT NULL`. Verifies the schema
    contract that justifies clamping on evaluation_time without NULL handling.
    """
    conn = sqlite3.connect(f"file:{cross_regime_snapshot}?mode=ro", uri=True)
    try:
        cols = {row[1]: row for row in conn.execute("PRAGMA table_info(evaluated_opportunities)")}
        eval_time_col = cols["evaluation_time"]
        # PRAGMA columns: (cid, name, type, notnull, dflt_value, pk)
        assert eval_time_col[3] == 1, (
            f"D-20 schema: evaluation_time should be NOT NULL, got notnull={eval_time_col[3]}"
        )
        settled_col = cols["settled_at"]
        assert settled_col[3] == 0, (
            f"D-20 schema: settled_at should be nullable, got notnull={settled_col[3]}"
        )
    finally:
        conn.close()
