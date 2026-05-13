"""P1.2 dashboard cohort_attribution panel contract pins (Phase 1, Money Printer Roadmap).

Sister Bit: ClickUp `86b9x3kj2`. Sister design: `kb/decisions/cohort-measurement-design-may12.md`
§ Dashboard mock + § Bit P1.2.

These tests pin the shape + behavior of the `cohort_attribution` top-level
key written by `DashboardSnapshotBuilder._build_snapshot`. Implementation
lives in `bot.snapshots.dashboard_snapshot._build_cohort_attribution_snap`
(module-level helper, mirrors the `_build_hourly_variant_snap` precedent).

Per the file-classification rule (tests/CLAUDE.md): behavioral, real-DB,
multi-row aggregation → integration tier.
"""
from __future__ import annotations

import datetime as _dt
import sqlite3
from typing import Any, Dict


# ─── Test fixtures ────────────────────────────────────────────────────────────


def _mk_conn() -> sqlite3.Connection:
    """In-memory conn with sqlite3.Row factory — production reads use named
    access (`row["asset"]`), so tests must too. Mirrors `bot/state.py` setup.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn


def _seed_cohort_row(conn: sqlite3.Connection, **overrides: Any) -> None:
    """Insert one `cohort_attribution_daily` row using reasonable defaults.

    Defaults assemble a non-bleed candidate cohort (n=100, cf_pnl positive).
    Override individual fields per-test to construct bleeders / cal-drift /
    cell-block stages without rewriting the full INSERT each time.
    """
    defaults: Dict[str, Any] = {
        "cohort_date": "2026-05-12",
        "asset": "SOL", "product_type": "15m", "strategy": "MAKER_PATIENT",
        "price_band_5c": 17, "stc_band_60s": 4, "cell_block_stage": "candidate",
        "n_30d": 100, "n_yes_30d": 90, "n_no_30d": 10,
        "wr_30d": 0.90, "wilson95_lo_30d": 0.83, "wilson95_hi_30d": 0.94,
        "sum_cf_cents_30d": 5000, "cf_pnl_30d_dollars": 50.0,
        "mean_cal_prob_30d": 0.92, "cal_gap_30d": 0.02,
        "n_7d": 25, "wr_7d": 0.88, "cf_pnl_7d_dollars": 12.0, "cal_gap_7d": 0.04,
        "alert_state": "quiet", "last_alert_time": None,
    }
    defaults.update(overrides)
    cols = list(defaults.keys())
    values = [defaults[c] for c in cols]
    placeholders = ",".join("?" for _ in cols)
    conn.execute(
        f"INSERT OR REPLACE INTO cohort_attribution_daily ({','.join(cols)}) "
        f"VALUES ({placeholders})",
        values,
    )


# ─── Pin 1 — snap shape ──────────────────────────────────────────────────────


def test_panel_has_required_top_level_keys():
    """Per design § Dashboard mock the panel exposes:
    `as_of`, `schema_version=1`, `summary`, `top_bleeders_30d`,
    `top_cal_drift_30d`.

    Empty table is allowed and exercises the no-data path; key set must
    be present unconditionally so the gh-pages renderer can rely on the
    schema regardless of cohort state.
    """
    from bot.helpers.cohort_attribution import ensure_schema
    from bot.snapshots.dashboard_snapshot import _build_cohort_attribution_snap

    conn = _mk_conn()
    ensure_schema(conn)
    panel = _build_cohort_attribution_snap(conn)

    assert "as_of" in panel
    assert panel.get("schema_version") == 1
    assert "summary" in panel
    assert "top_bleeders_30d" in panel
    assert "top_cal_drift_30d" in panel
    assert isinstance(panel["top_bleeders_30d"], list)
    assert isinstance(panel["top_cal_drift_30d"], list)
    assert isinstance(panel["summary"], dict)


# ─── Pin 2 — top_bleeders_30d sort order ─────────────────────────────────────


def test_top_bleeders_30d_sorted_by_cf_pnl_ascending():
    """`top_bleeders_30d` ranks cohorts by `cf_pnl_30d` ASC — the
    most-negative-pnl bleeder appears first per design mock.

    Three seeded rows: SOL bleed (-$285), ETH bleed (-$80), BTC positive
    (+$50). The positive row MUST be excluded from bleeders entirely
    (bleeders only — `cf_pnl_30d_dollars < 0`)."""
    from bot.helpers.cohort_attribution import ensure_schema
    from bot.snapshots.dashboard_snapshot import _build_cohort_attribution_snap

    conn = _mk_conn()
    ensure_schema(conn)
    _seed_cohort_row(
        conn, asset="ETH", strategy="MAKER_PATIENT", price_band_5c=15,
        n_30d=80, wr_30d=0.85, wilson95_hi_30d=0.88, cf_pnl_30d_dollars=-80.0,
        sum_cf_cents_30d=-8000,
    )
    _seed_cohort_row(
        conn, asset="SOL", strategy="MAKER_PATIENT", price_band_5c=16,
        n_30d=138, wr_30d=0.841, wilson95_hi_30d=0.892, cf_pnl_30d_dollars=-285.0,
        sum_cf_cents_30d=-28500,
    )
    _seed_cohort_row(
        conn, asset="BTC", strategy="MAKER_PATIENT", price_band_5c=18,
        n_30d=100, wr_30d=0.99, wilson95_hi_30d=0.999, cf_pnl_30d_dollars=+50.0,
        sum_cf_cents_30d=5000,
    )

    panel = _build_cohort_attribution_snap(conn)
    bleeders = panel["top_bleeders_30d"]

    assert len(bleeders) == 2, "BTC positive cf_pnl must be excluded from bleeders"
    assert bleeders[0]["cf_pnl_30d"] == -285.0
    assert bleeders[1]["cf_pnl_30d"] == -80.0
    for b in bleeders:
        assert b["cf_pnl_30d"] < 0


# ─── Pin 3 — summary count parity ────────────────────────────────────────────


def test_summary_n_cohorts_total_matches_distinct_count():
    """`summary.n_cohorts_total` equals SELECT COUNT(DISTINCT cohort_key)
    over the LATEST `cohort_date` only — earlier-date rows are excluded.

    The latest cohort_date in the seeded fixture is `2026-05-12` (3 distinct
    cohorts). One row on an earlier `cohort_date=2026-05-11` must NOT inflate
    the count.
    """
    from bot.helpers.cohort_attribution import ensure_schema
    from bot.snapshots.dashboard_snapshot import _build_cohort_attribution_snap

    conn = _mk_conn()
    ensure_schema(conn)
    _seed_cohort_row(conn, asset="SOL", strategy="MAKER_PATIENT", cohort_date="2026-05-12")
    _seed_cohort_row(conn, asset="ETH", strategy="MAKER_PATIENT", cohort_date="2026-05-12")
    _seed_cohort_row(conn, asset="BTC", strategy="TAKER_NOW", cohort_date="2026-05-12")
    _seed_cohort_row(conn, asset="XRP", strategy="MAKER_PATIENT", cohort_date="2026-05-11")

    panel = _build_cohort_attribution_snap(conn)
    assert panel["summary"]["n_cohorts_total"] == 3
    assert panel["summary"]["latest_cohort_date"] == "2026-05-12"


# ─── Pin 4 — UNION set respected ─────────────────────────────────────────────


def test_union_partition_stages_visible_in_panel():
    """Both `candidate` baseline rows AND cell-block stage rows
    (`SOL_BLEED_V2_88_93C_2_5MIN`, `TM98_97_98C_2_5MIN_BLEED`, etc.)
    are read by the panel. The dashboard surfaces blocks-in-effect AS
    WELL AS still-bleeding candidate rollups so the operator can see
    both sides of the UNION.

    Empirically the cell-block rows have positive cf_pnl_30d after the
    block fires (it's no longer bleeding); we seed them with NEGATIVE
    cf_pnl to verify the panel includes them in `top_bleeders_30d` —
    i.e., the bleeder filter is NOT stage-restricted. (P1.3 alert
    triggers DO restrict to `candidate`; P1.2 dashboard surfaces both.)
    """
    from bot.helpers.cohort_attribution import ensure_schema
    from bot.snapshots.dashboard_snapshot import _build_cohort_attribution_snap

    conn = _mk_conn()
    ensure_schema(conn)
    _seed_cohort_row(
        conn, asset="SOL", strategy="MAKER_PATIENT", price_band_5c=17,
        cell_block_stage="candidate",
        n_30d=138, wr_30d=0.841, wilson95_hi_30d=0.892,
        cf_pnl_30d_dollars=-285.0, sum_cf_cents_30d=-28500,
    )
    _seed_cohort_row(
        conn, asset="SOL", strategy="MAKER_PATIENT", price_band_5c=18,
        cell_block_stage="SOL_BLEED_V2_88_93C_2_5MIN",
        n_30d=80, wr_30d=0.80, wilson95_hi_30d=0.88,
        cf_pnl_30d_dollars=-50.0, sum_cf_cents_30d=-5000,
    )

    panel = _build_cohort_attribution_snap(conn)
    stages_seen = {b["stage"] for b in panel["top_bleeders_30d"]}
    assert "candidate" in stages_seen
    assert "SOL_BLEED_V2_88_93C_2_5MIN" in stages_seen


# ─── Pin 5 — pagination shape ────────────────────────────────────────────────


def test_pagination_caps_bleeders_and_cal_drift_at_20():
    """`top_bleeders_30d` and `top_cal_drift_30d` each cap at 20 entries
    so the snapshot payload (-> Supabase row) stays bounded.

    Seeds 25 cohorts that all qualify as both bleeders AND cal-drift —
    the panel must return at most 20 of each.
    """
    from bot.helpers.cohort_attribution import ensure_schema
    from bot.snapshots.dashboard_snapshot import _build_cohort_attribution_snap

    conn = _mk_conn()
    ensure_schema(conn)
    for i in range(25):
        _seed_cohort_row(
            conn,
            asset="SOL", strategy="MAKER_PATIENT",
            price_band_5c=i + 1,  # distinct cohort key
            stc_band_60s=4,
            n_30d=100, wr_30d=0.80,
            wilson95_hi_30d=0.85,
            cf_pnl_30d_dollars=-50.0 - i,
            sum_cf_cents_30d=-5000 - i * 100,
            mean_cal_prob_30d=0.95, cal_gap_30d=0.15 + i * 0.001,
        )

    panel = _build_cohort_attribution_snap(conn)
    assert len(panel["top_bleeders_30d"]) <= 20
    assert len(panel["top_cal_drift_30d"]) <= 20


# ─── Pin 6 — top_cal_drift_30d sort order (R1 MN1) ───────────────────────────


def test_top_cal_drift_30d_sorted_by_abs_cal_gap_descending():
    """`top_cal_drift_30d` ranks cohorts by `|cal_gap_30d|` DESC — the
    largest-magnitude drift surfaces first regardless of gap sign.

    A regression that dropped the `ABS()` in the ORDER BY (i.e., ranked
    on signed `cal_gap_30d DESC`) would silently surface only positive-
    drift cohorts and hide negative-drift bleeders. Seeds 3 mixed-sign
    rows; the row with `cal_gap=-0.18` MUST be ranked first (magnitude
    larger than +0.10 and +0.05).
    """
    from bot.helpers.cohort_attribution import ensure_schema
    from bot.snapshots.dashboard_snapshot import _build_cohort_attribution_snap

    conn = _mk_conn()
    ensure_schema(conn)
    _seed_cohort_row(
        conn, asset="ETH", strategy="MAKER_PATIENT", price_band_5c=15,
        n_30d=70, wr_30d=0.85, mean_cal_prob_30d=0.95, cal_gap_30d=0.10,
    )
    _seed_cohort_row(
        conn, asset="SOL", strategy="MAKER_PATIENT", price_band_5c=16,
        n_30d=80, wr_30d=0.95, mean_cal_prob_30d=0.77, cal_gap_30d=-0.18,
    )
    _seed_cohort_row(
        conn, asset="BTC", strategy="TAKER_NOW", price_band_5c=18,
        n_30d=60, wr_30d=0.90, mean_cal_prob_30d=0.95, cal_gap_30d=0.05,
    )

    panel = _build_cohort_attribution_snap(conn)
    drift = panel["top_cal_drift_30d"]
    assert len(drift) == 3
    assert drift[0]["cal_gap"] == -0.18  # largest magnitude regardless of sign
    assert abs(drift[1]["cal_gap"]) == 0.10
    assert abs(drift[2]["cal_gap"]) == 0.05


# ─── Pin 7 — outer try/except fallback (R1 MN5) ──────────────────────────────


def test_inner_helper_and_outer_fallback_share_shape_via_constant():
    """The empty-panel shape returned by `_build_cohort_attribution_snap`
    on the no-data path AND the shape written by the wire-in's
    `except` branch MUST come from a single source — the
    `_empty_cohort_panel(now_iso)` helper. Importing the helper and
    cross-pinning against the inner-helper output is load-bearing: a
    regression that diverged the two paths (e.g., dropping `stale` from
    the except branch) would fail this pin because both call sites now
    delegate to the same constructor.

    Closes R2 MN1: the prior literal-cross-copy pin would silently
    accept lockstep drift if both sides got edited together by a buggy
    change. Routing through `_empty_cohort_panel` removes that loophole.
    """
    from bot.helpers.cohort_attribution import ensure_schema
    from bot.snapshots.dashboard_snapshot import (
        _build_cohort_attribution_snap,
        _empty_cohort_panel,
    )

    conn = _mk_conn()
    ensure_schema(conn)
    inner = _build_cohort_attribution_snap(conn)
    fallback = _empty_cohort_panel("sentinel-as_of")

    # Top-level + summary key sets identical between the no-data path
    # and the helper-raises fallback path (both built via _empty_cohort_panel).
    assert set(inner.keys()) == set(fallback.keys())
    assert set(inner["summary"].keys()) == set(fallback["summary"].keys())
    # Both empty-list lists empty + schema_version pinned.
    assert inner["top_bleeders_30d"] == fallback["top_bleeders_30d"] == []
    assert inner["top_cal_drift_30d"] == fallback["top_cal_drift_30d"] == []
    assert inner["schema_version"] == fallback["schema_version"] == 1
    # Stale=True on both empty paths.
    assert inner["summary"]["stale"] is True
    assert fallback["summary"]["stale"] is True


# ─── Pin 8 — R7 stale-snapshot fallback advisory ─────────────────────────────


def test_stale_snapshot_surfaces_stale_flag():
    """R7 advisory pass-forward (cohort-measurement design § P1.4): when
    the latest `cohort_date` is older than ~36h the nightly cron missed,
    so the panel surfaces `stale=True` rather than silently rendering
    yesterday's snapshot as if it were fresh.

    Cleared-table case (no rows) MUST also report `stale=True` with
    `latest_cohort_date=None` so the operator sees the gap explicitly.
    """
    from bot.helpers.cohort_attribution import ensure_schema
    from bot.snapshots.dashboard_snapshot import _build_cohort_attribution_snap

    # Case A: empty table — stale True, latest_cohort_date None
    conn = _mk_conn()
    ensure_schema(conn)
    panel = _build_cohort_attribution_snap(conn)
    assert panel["summary"]["stale"] is True
    assert panel["summary"]["latest_cohort_date"] is None

    # Case B: latest row is 3 days stale
    stale_date = (
        _dt.datetime.now(_dt.timezone.utc).date() - _dt.timedelta(days=3)
    ).isoformat()
    _seed_cohort_row(conn, cohort_date=stale_date)
    panel = _build_cohort_attribution_snap(conn)
    assert panel["summary"]["stale"] is True
    assert panel["summary"]["latest_cohort_date"] == stale_date
