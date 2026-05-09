"""D-1: cf_pnl per-row identity (the validation gate).

Replay must reproduce evaluated_opportunities.counterfactual_pnl byte-for-byte
against the snapshot DB. This is THE gate of the replay engine — without it,
no other replayed quantity is meaningful.

Scope: rows from 2026-05-03 onwards. The snapshot contains rows back to
2026-02-21, but the cf_pnl formula evolved over time (taker fees were added
to the computation; weather untradeable_price branch was added). Replay byte-
equality is required for the CURRENT canonical formula, which is also the
relevant era for v3 training (live_ws cutover was 2026-05-02T19:53:19Z).

Pre-canonical rows are documented as a known limitation in
kb/decisions/replay-engine-execution-plan-may09.md and not in scope for D-1.

See kb/decisions/replay-engine-rca-2026-05-05.md (D-1) and
kb/decisions/replay-engine-execution-plan-may09.md.
"""
from __future__ import annotations

import sqlite3

import pytest

from research.replay import replay_cf_pnl


# Canonical-formula era cutoff. Rows before this may use older cf_pnl formulas
# (no fee deduction, no weather untradeable_price branch). Choose the live_ws
# cutover boundary plus a small buffer for safety.
CANONICAL_FORMULA_CUTOFF = "2026-05-03T00:00:00Z"


def _fetch_settled_rows(conn: sqlite3.Connection):
    """Settled-row population for the D-1 byte-equality gate.

    Exclusion: market_price > 0. Per B1 spike, the snapshot's canonical
    era contains a population of `market_price=0` rows that ALL share
    filter_stage='price_out_of_range' + side=yes + market_result=no,
    and is empirically 15M-only in observed snapshots (recheck on new
    asset onboarding — see `agent_docs/asset-onboarding-doge-hype-spike.md`).
    A trade at entry=0c is non-tradeable on Kalshi (sub-1c orders
    rejected) so these rows don't represent meaningful
    would-have-been-trades regardless of the precise filtering mechanism.

    The population is dominated by `cf_pnl=0` (canonical formula on
    entry=0 LOSS yields 0). Exactly ONE row (id=493137 KXSOL15M-26MAY031900-00)
    carries cf_pnl=-62, the only divergent value. -62 = -(60×1 + 2-fee)
    matches a 60c-entry LOSS — but the stored `market_price` is 0, not 60.
    `_poll_evaluated_opportunities` reads `row["market_price"]` at
    settlement time, and no production writer overwrites market_price
    post-insert, so canonical-formula application against the stored
    row should yield cf_pnl=0 here, not -62. The divergence is unexplained
    from the snapshot alone; the exact mechanism cannot be reconstructed
    without journal-level forensics.

    Note: the underlying eval-after-settle race condition (eval_time >
    settled_time, indicating bot polled a market that had just closed)
    has been observed >=2× in the canonical era as of snapshot
    20260508; the OTHER occurrence (id=515308 ETH 2026-05-05, mp=100,
    side=yes, result=yes) is canonical-correct (cf_pnl=0 matches
    formula on a 100c-entry WIN: (100-100)×1 - 0 = 0) so it doesn't
    surface as a divergent cf_pnl. The race is real but only divergent
    when it lands on a stale-price coincidence.
    """
    sql = """
        SELECT id, ticker, asset, product_type, side, market_price,
               position_size, market_result, counterfactual_pnl
        FROM evaluated_opportunities
        WHERE status = 'settled'
          AND market_result IS NOT NULL
          AND evaluation_time >= ?
          AND market_price > 0
    """
    return list(conn.execute(sql, (CANONICAL_FORMULA_CUTOFF,)))


def test_d01_snapshot_has_settled_rows(snapshot_conn):
    """Sanity: the snapshot DB actually contains settled rows to validate against."""
    rows = _fetch_settled_rows(snapshot_conn)
    assert len(rows) > 0, "snapshot has no settled evaluated_opportunities rows"


def test_d01_cf_pnl_byte_equality_settled(snapshot_conn):
    """Replay must reproduce stored counterfactual_pnl byte-for-byte where stored.

    Skips rows where stored cf_pnl is NULL (live wrote None for those — typically
    unknown_result_* or unknown_no_price). Those are covered by the NULL test below.

    market_price=0 rows are excluded at the SQL level (see _fetch_settled_rows
    docstring for the B1 spike's full reasoning).
    """
    rows = _fetch_settled_rows(snapshot_conn)
    mismatches: list[tuple] = []
    checked = 0
    for r in rows:
        stored = r["counterfactual_pnl"]
        if stored is None:
            continue
        replayed = replay_cf_pnl(
            entry_price=r["market_price"],
            market_result=r["market_result"],
            side=r["side"],
            position_size=r["position_size"],
            product_type=r["product_type"],
        )
        checked += 1
        if replayed != stored:
            mismatches.append((r["id"], r["ticker"], r["product_type"], r["side"],
                               r["market_price"], r["position_size"],
                               r["market_result"], stored, replayed))
        if len(mismatches) >= 20:
            break
    assert checked > 0, "no non-null cf_pnl rows checked — fixture is wrong"
    assert not mismatches, (
        f"{len(mismatches)} cf_pnl mismatches (showing first 20):\n"
        + "\n".join(
            f"  id={m[0]} {m[1]} pt={m[2]} side={m[3]} entry={m[4]} size={m[5]} "
            f"result={m[6]} stored={m[7]} replayed={m[8]}"
            for m in mismatches
        )
    )


def test_d01_excluded_zero_price_population_is_bounded(snapshot_conn):
    """Sanity bound on the excluded-by-_fetch_settled_rows population.

    The market_price=0 exclusion (B1 spike) is justified only as long as
    the excluded population stays a small fraction of the canonical-era
    settled population AND the cf_pnl != 0 anomaly count stays at most 1.
    If either grows, the exclusion masks a real divergence source and
    must be revisited.

    R1 review: original abs-cap of 500 would have been hit in ~18 days
    at observed growth rate (~21 zero-price rows/day). Switched to a
    ratio bound (≤2% of canonical-era settled) which self-scales with
    snapshot age, plus a generous absolute cap as a belt-and-suspenders.
    """
    excluded_sql = """
        SELECT
          COUNT(*) AS total_excluded,
          SUM(CASE WHEN counterfactual_pnl IS NULL THEN 1 ELSE 0 END) AS null_cf,
          SUM(CASE WHEN counterfactual_pnl = 0 THEN 1 ELSE 0 END) AS zero_cf,
          SUM(CASE WHEN counterfactual_pnl IS NOT NULL
                   AND counterfactual_pnl != 0 THEN 1 ELSE 0 END) AS nonzero_cf
        FROM evaluated_opportunities
        WHERE status = 'settled'
          AND market_result IS NOT NULL
          AND evaluation_time >= ?
          AND market_price = 0
    """
    total_sql = """
        SELECT COUNT(*) AS n
        FROM evaluated_opportunities
        WHERE status = 'settled'
          AND market_result IS NOT NULL
          AND evaluation_time >= ?
    """
    excluded = snapshot_conn.execute(
        excluded_sql, (CANONICAL_FORMULA_CUTOFF,)
    ).fetchone()
    total = snapshot_conn.execute(
        total_sql, (CANONICAL_FORMULA_CUTOFF,)
    ).fetchone()["n"]
    assert total > 0, "fixture: no canonical-era settled rows at all"
    excluded_ratio = excluded["total_excluded"] / total

    # Ratio bound — self-scales with snapshot age. Observed ratios:
    # snapshot 20260505_2157 → 67/8132 ≈ 0.82%
    # snapshot 20260508       → 125/16083 ≈ 0.78%
    # Steady-state ~0.8%; cap at 2% leaves ~2.5× headroom for shocks.
    assert excluded_ratio <= 0.02, (
        f"market_price=0 excluded ratio is {excluded_ratio:.3%} "
        f"({excluded['total_excluded']} of {total} canonical-era settled "
        f"rows) — exceeds the 2% cap; B1 exclusion may be masking a real "
        "divergence source; investigate."
    )
    # Belt-and-suspenders abs cap: even if the ratio passes, an exclusion
    # population in the tens of thousands deserves a re-look. 5000 is
    # generous (~10 months of headroom at current ~21/day growth).
    assert excluded["total_excluded"] <= 5000, (
        f"market_price=0 excluded count is {excluded['total_excluded']} — "
        "exceeds the absolute cap; investigate even if the ratio passes."
    )
    # The known anomaly: exactly 1 row with cf_pnl != 0 (id=493137).
    # Allowed values: 0 (the row aged out of the canonical-era window,
    # e.g. if CANONICAL_FORMULA_CUTOFF moves past 2026-05-03) or 1 (the
    # same row still in window). >1 means new instances of the
    # eval-after-settle race coincided with stale-price snapshots and
    # warrants re-investigation.
    assert excluded["nonzero_cf"] in (0, 1), (
        f"{excluded['nonzero_cf']} market_price=0 rows have non-zero cf_pnl — "
        "B1 documented exactly 1 (id=493137); a higher count means the "
        "eval-after-settle race is producing divergent cf_pnl recurrently "
        "and the exclusion no longer covers it cleanly."
    )


def test_d01_cf_pnl_null_when_live_was_null(snapshot_conn):
    """Where live wrote NULL cf_pnl, replay must also produce None.

    Live writes None when entry_price is NULL OR market_result is unknown.
    """
    sql = """
        SELECT id, ticker, asset, product_type, side, market_price,
               position_size, market_result
        FROM evaluated_opportunities
        WHERE status = 'settled'
          AND counterfactual_pnl IS NULL
          AND market_result IS NOT NULL
          AND evaluation_time >= ?
        LIMIT 500
    """
    rows = list(snapshot_conn.execute(sql, (CANONICAL_FORMULA_CUTOFF,)))
    if not rows:
        pytest.skip("no NULL cf_pnl rows in snapshot to validate")
    mismatches = []
    for r in rows:
        replayed = replay_cf_pnl(
            entry_price=r["market_price"],
            market_result=r["market_result"],
            side=r["side"],
            position_size=r["position_size"],
            product_type=r["product_type"],
        )
        if replayed is not None:
            mismatches.append((r["id"], r["ticker"], r["market_price"],
                               r["market_result"], replayed))
        if len(mismatches) >= 10:
            break
    assert not mismatches, (
        f"{len(mismatches)} rows where live wrote NULL but replay produced a value:\n"
        + "\n".join(
            f"  id={m[0]} {m[1]} entry={m[2]} result={m[3]} replayed={m[4]}"
            for m in mismatches
        )
    )
