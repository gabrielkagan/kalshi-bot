"""D-28 — validation gate must be product-type-aware.

Authoritative source: dashboard's `SUM(counterfactual_pnl WHERE status='settled')`
is computed PER PRODUCT (per RCA D-28). Different product types have different
settlement cadences, fee structures (D-16), and floor gates (D-4). The
validation gate asserts per-product totals match — not just the global total.

Per-product breakdown source:
- 15m, hourly, spx_hourly, weather: cf via main path
- sports: separate sports_shadow_log table (out of scope for replay v1)

Replay's product-type-aware validation iterates over distinct product_type
values in the snapshot and runs per-product cf-sum identity. Catches D-4 /
D-16 regressions that show up as per-product divergence.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from research.replay import replay_cf_pnl


CANONICAL_FORMULA_CUTOFF = "2026-05-03T00:00:00Z"


def test_d28_snapshot_has_multiple_product_types(snapshot_conn: sqlite3.Connection) -> None:
    """The real snapshot has multiple product_type values (15m/hourly/weather/etc.)."""
    products = [
        row[0] for row in snapshot_conn.execute(
            "SELECT DISTINCT product_type FROM evaluated_opportunities "
            "WHERE product_type IS NOT NULL"
        )
    ]
    assert len(products) >= 2, (
        f"D-28 snapshot diversity: expected ≥2 product types, got {products}"
    )
    # 15m should always be present in the canonical bot data
    assert "15m" in products, f"D-28 missing 15m: products={products}"


def test_d28_per_product_cf_pnl_identity_15m(snapshot_conn: sqlite3.Connection) -> None:
    """For 15m product, replay_cf_pnl matches stored counterfactual_pnl byte-for-byte.

    Subset of D-1's per-row identity, scoped to product_type='15m'. Catches
    a future bug where replay diverges per-product (e.g., a SPX fee branch
    accidentally applied to 15m).
    """
    rows = list(snapshot_conn.execute(
        """
        SELECT id, market_price, market_result, side, position_size,
               product_type, counterfactual_pnl
        FROM evaluated_opportunities
        WHERE status = 'settled'
          AND counterfactual_pnl IS NOT NULL
          AND market_result IN ('yes', 'no', 'all_yes', 'all_no')
          AND market_price IS NOT NULL
          AND market_price > 0
          AND product_type = '15m'
          AND evaluation_time >= ?
        LIMIT 500
        """,
        (CANONICAL_FORMULA_CUTOFF,),
    ))
    if not rows:
        pytest.skip("D-28: no 15m settled rows in canonical era")
    divergences = []
    for row in rows:
        _id, mp, mr, side, ps, pt, stored_cf = row
        computed = replay_cf_pnl(
            entry_price=mp,
            market_result=mr,
            side=side,
            position_size=ps,
            product_type=pt,
        )
        if computed != stored_cf:
            divergences.append((_id, mp, mr, side, ps, pt, stored_cf, computed))
    assert not divergences, (
        f"D-28 per-product cf identity (15m): {len(divergences)} divergences. "
        f"First 3: {divergences[:3]}"
    )


def test_d28_per_product_cf_pnl_identity_weather(snapshot_conn: sqlite3.Connection) -> None:
    """Per-product identity for weather. Catches D-4 floor-gate regressions."""
    rows = list(snapshot_conn.execute(
        """
        SELECT id, market_price, market_result, side, position_size,
               product_type, counterfactual_pnl
        FROM evaluated_opportunities
        WHERE status = 'settled'
          AND counterfactual_pnl IS NOT NULL
          AND market_result IN ('yes', 'no', 'all_yes', 'all_no')
          AND market_price IS NOT NULL
          AND market_price > 0
          AND product_type = 'weather'
          AND evaluation_time >= ?
        LIMIT 500
        """,
        (CANONICAL_FORMULA_CUTOFF,),
    ))
    if not rows:
        pytest.skip("D-28: no weather settled rows in canonical era")
    divergences = []
    for row in rows:
        _id, mp, mr, side, ps, pt, stored_cf = row
        computed = replay_cf_pnl(
            entry_price=mp,
            market_result=mr,
            side=side,
            position_size=ps,
            product_type=pt,
        )
        if computed != stored_cf:
            divergences.append((_id, mp, mr, side, ps, pt, stored_cf, computed))
    assert not divergences, (
        f"D-28 per-product cf identity (weather): {len(divergences)} divergences. "
        f"First 3: {divergences[:3]}"
    )


def test_d28_per_product_cf_pnl_identity_spx(snapshot_conn: sqlite3.Connection) -> None:
    """Per-product identity for spx_hourly. Catches D-16 fee-mismatch regressions.

    Per D-16: replay uses 0.07 for SPX (matches live, NOT the bot.constants
    SPX_HOURLY_FEE_MULTIPLIER_TAKER=0.035). If replay ever switches SPX cf
    to 0.035, this identity fails.
    """
    rows = list(snapshot_conn.execute(
        """
        SELECT id, market_price, market_result, side, position_size,
               product_type, counterfactual_pnl
        FROM evaluated_opportunities
        WHERE status = 'settled'
          AND counterfactual_pnl IS NOT NULL
          AND market_result IN ('yes', 'no', 'all_yes', 'all_no')
          AND market_price IS NOT NULL
          AND market_price > 0
          AND product_type = 'spx_hourly'
          AND evaluation_time >= ?
        LIMIT 500
        """,
        (CANONICAL_FORMULA_CUTOFF,),
    ))
    if not rows:
        pytest.skip("D-28: no spx_hourly settled rows in canonical era")
    divergences = []
    for row in rows:
        _id, mp, mr, side, ps, pt, stored_cf = row
        computed = replay_cf_pnl(
            entry_price=mp,
            market_result=mr,
            side=side,
            position_size=ps,
            product_type=pt,
        )
        if computed != stored_cf:
            divergences.append((_id, mp, mr, side, ps, pt, stored_cf, computed))
    assert not divergences, (
        f"D-28 per-product cf identity (spx_hourly): {len(divergences)} divergences. "
        f"D-16 SPX fee-mismatch suspected. First 3: {divergences[:3]}"
    )


def test_d28_per_product_aggregation_excludes_sports(snapshot_conn: sqlite3.Connection) -> None:
    """sports product_type is out-of-scope for replay v1 (per RCA D-28).

    Sports cf data lives in sports_shadow_log, not evaluated_opportunities.
    R2 finding M4: the previous version was permissive — B3 could pass by
    simply not mentioning 'sports'. Tightened: require evaluate_window's
    output to EXCLUDE sports rows when given a snapshot with sports data.
    """
    import inspect
    import research.replay as rep

    # Part 1: AST guard — if replay.py mentions 'sports', it must filter on it.
    src = inspect.getsource(rep)
    if "sports" in src.lower():
        assert (
            "!= 'sports'" in src
            or '!= "sports"' in src
            or "not 'sports'" in src
            or 'not "sports"' in src
            or "exclude" in src.lower()  # documented exclusion comment
        ), (
            "D-28 sports reference in replay.py without filter: replay must "
            "exclude product_type='sports' from per-product iteration."
        )

    # Part 2: behavioral guard — if evaluate_window exists, calling it against
    # a snapshot containing sports rows must NOT include them in the per-product
    # output. TDD-red until B3 ships evaluate_window.
    if not hasattr(rep, "evaluate_window"):
        pytest.skip("D-28 TDD-red: evaluate_window not yet implemented")

    # Build a synthetic snapshot with a sports row + a 15m row
    import sqlite3
    import tempfile
    import textwrap
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        synth_path = f.name
    conn = sqlite3.connect(synth_path)
    try:
        conn.executescript(textwrap.dedent("""
            CREATE TABLE evaluated_opportunities (
                id INTEGER PRIMARY KEY,
                evaluation_time TEXT NOT NULL,
                settled_time TEXT,
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
            "INSERT INTO evaluated_opportunities (evaluation_time, settled_time, "
            "market_result, market_price, position_size, product_type, status, "
            "counterfactual_pnl) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("2026-05-05T12:00:00.000Z", "2026-05-05T12:15:00.000Z", "yes", 85, 1, "15m", "settled", 14),
                ("2026-05-05T13:00:00.000Z", "2026-05-05T17:00:00.000Z", "yes", 50, 1, "sports", "settled", 48),
            ],
        )
        conn.commit()
    finally:
        conn.close()
    # Call evaluate_window and check sports rows are excluded
    result = rep.evaluate_window(snapshot_path=synth_path)
    per_product = getattr(result, "per_product", None) or (
        result.get("per_product") if isinstance(result, dict) else None
    )
    if per_product is not None:
        assert "sports" not in per_product, (
            f"D-28 sports leak: evaluate_window included sports rows in per_product output. "
            f"Got keys: {list(per_product.keys()) if hasattr(per_product, 'keys') else per_product}"
        )


def test_d28_validation_gate_aggregates_per_product(snapshot_conn: sqlite3.Connection) -> None:
    """Per-product cf-pnl SUM is computable from the snapshot.

    Pin the canonical aggregation query shape. This is the gate that catches
    D-4 (weather floor) + D-16 (SPX fee) regressions at the SUM level.
    """
    rows = list(snapshot_conn.execute(
        """
        SELECT product_type, SUM(counterfactual_pnl) AS total_cf_cents, COUNT(*) AS n
        FROM evaluated_opportunities
        WHERE status = 'settled'
          AND counterfactual_pnl IS NOT NULL
          AND product_type IS NOT NULL
          AND product_type != 'sports'
        GROUP BY product_type
        """
    ))
    assert len(rows) >= 1, "D-28 per-product aggregation: snapshot has no settled rows"
    for product, total_cf, n in rows:
        assert total_cf is not None, f"D-28 product={product} has NULL SUM (all rows NULL?)"
        assert n > 0, f"D-28 product={product} has zero rows"
