"""P1.4 weekly bleed report contract pins (Phase 1, Money Printer Roadmap).

Sister Bit: ClickUp `86b9x3kn2`. Sister design:
`kb/decisions/cohort-measurement-design-may12.md` § Weekly bleed report +
§ Bit P1.4.

These tests pin the shape + behavior of the markdown report written by
`scripts/audit/weekly_bleed_report.py` on Mondays at 13:13 UTC. The
report reads `cohort_attribution_daily` (P1.1) + raw counts from
`evaluated_opportunities` / `rejected_opportunities` for the coverage-
stats section, and emits a Telegram one-liner via the canonical
`bot.notifier._TELEGRAM` singleton.

Per the file-classification rule (tests/CLAUDE.md): behavioral, real-
DB, multi-row aggregation -> integration tier.
"""
from __future__ import annotations

import datetime as _dt
import sqlite3
from pathlib import Path
from typing import Any, Dict, List

import pytest


# ─── Test fixtures ────────────────────────────────────────────────────────────


def _mk_conn() -> sqlite3.Connection:
    """In-memory conn with sqlite3.Row factory — production reads use named
    access (`row["asset"]`)."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn


def _seed_cohort_row(conn: sqlite3.Connection, **overrides: Any) -> None:
    """Insert one `cohort_attribution_daily` row with reasonable defaults."""
    defaults: Dict[str, Any] = {
        "cohort_date": "2026-05-18",
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


def _seed_evaluated_opportunities_table(conn: sqlite3.Connection) -> None:
    """Stripped-down evaluated_opportunities for coverage-stats math."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS evaluated_opportunities (
            id INTEGER PRIMARY KEY,
            asset TEXT, product_type TEXT, strategy TEXT,
            market_price INTEGER, seconds_to_close REAL,
            filter_stage TEXT, market_result TEXT,
            calibrated_prob REAL, counterfactual_pnl INTEGER,
            evaluation_time TEXT
        )
        """
    )


def _seed_rejected_opportunities_table(conn: sqlite3.Connection) -> None:
    """Stripped-down rejected_opportunities for coverage-stats math."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS rejected_opportunities (
            id INTEGER PRIMARY KEY,
            asset TEXT, evaluation_time TEXT
        )
        """
    )


def _seed_admit(
    conn: sqlite3.Connection,
    *,
    filter_stage: str,
    eval_time: str,
    asset: str = "SOL",
) -> None:
    conn.execute(
        "INSERT INTO evaluated_opportunities (asset, product_type, strategy, "
        "market_price, seconds_to_close, filter_stage, market_result, "
        "evaluation_time) VALUES (?, '15m', 'MAKER_PATIENT', 88, 240.0, ?, "
        "'yes', ?)",
        (asset, filter_stage, eval_time),
    )


def _seed_reject(conn: sqlite3.Connection, *, eval_time: str) -> None:
    conn.execute(
        "INSERT INTO rejected_opportunities (asset, evaluation_time) "
        "VALUES ('SOL', ?)",
        (eval_time,),
    )


# ─── Pin 1 — section coverage + frontmatter schema version ──────────────────


def test_report_has_5_design_sections():
    """All 5 sections per design § Weekly bleed report are present in
    the rendered markdown body, plus the frontmatter exposes
    `Schema version: 1` so future schema evolutions can be detected by
    the gh-pages renderer / external readers."""
    from bot.helpers.cohort_attribution import ensure_schema
    from scripts.audit.weekly_bleed_report import SCHEMA_VERSION, build_report

    conn = _mk_conn()
    ensure_schema(conn)
    _seed_evaluated_opportunities_table(conn)
    _seed_rejected_opportunities_table(conn)
    _seed_cohort_row(conn, cohort_date="2026-05-18")

    md = build_report(conn, report_date="2026-05-18")

    assert "## 1. Top 10 bleeders" in md
    assert "## 2. Top 10 cal-drift cohorts" in md
    assert "## 3. New cohorts crossing alert thresholds" in md
    assert "## 4. Cohorts that EXITED alert state" in md
    assert "## 5. Coverage stats" in md
    # R1 MN2: SCHEMA_VERSION constant is load-bearing for future evolution.
    assert f"**Schema version**: {SCHEMA_VERSION}" in md
    assert SCHEMA_VERSION == 1


# ─── Pin 2 — bleeders sort + positive excluded ───────────────────────────────


def test_top_bleeders_sorted_cf_pnl_ascending_and_excludes_positive():
    """Section 1 ranks cohorts by cf_pnl_30d_dollars ASC; positive-pnl
    rows MUST be excluded (bleeders only). A regression that dropped the
    `<0` predicate would surface profitable cohorts as "bleeders"."""
    from bot.helpers.cohort_attribution import ensure_schema
    from scripts.audit.weekly_bleed_report import build_report

    conn = _mk_conn()
    ensure_schema(conn)
    _seed_evaluated_opportunities_table(conn)
    _seed_rejected_opportunities_table(conn)

    _seed_cohort_row(
        conn, asset="ETH", strategy="MAKER_PATIENT", price_band_5c=15,
        cohort_date="2026-05-18", cf_pnl_30d_dollars=-80.0,
        sum_cf_cents_30d=-8000, wr_30d=0.85, wilson95_hi_30d=0.88,
    )
    _seed_cohort_row(
        conn, asset="SOL", strategy="MAKER_PATIENT", price_band_5c=16,
        cohort_date="2026-05-18", cf_pnl_30d_dollars=-285.0,
        sum_cf_cents_30d=-28500, wr_30d=0.841, wilson95_hi_30d=0.892,
    )
    _seed_cohort_row(
        conn, asset="BTC", strategy="TAKER_NOW", price_band_5c=18,
        cohort_date="2026-05-18", cf_pnl_30d_dollars=+50.0,
        sum_cf_cents_30d=5000,
    )

    md = build_report(conn, report_date="2026-05-18")

    bleeders_section = md.split("## 2.")[0]
    # SOL -$285 appears before ETH -$80
    sol_pos = bleeders_section.find("SOL")
    eth_pos = bleeders_section.find("ETH")
    assert sol_pos != -1 and eth_pos != -1
    assert sol_pos < eth_pos, "SOL (-$285) must rank above ETH (-$80) in bleeders"
    # BTC positive-pnl row must NOT appear in section 1
    assert "BTC" not in bleeders_section, (
        "Positive-cf_pnl rows must be excluded from the bleeders table"
    )


# ─── Pin 3 — cal-drift sort by |cal_gap| DESC ────────────────────────────────


def test_top_cal_drift_sorted_by_abs_cal_gap_descending():
    """Section 2 ranks cohorts by |cal_gap_30d| DESC — largest-magnitude
    drift first regardless of sign. Mirrors P1.2 dashboard panel pin 6."""
    from bot.helpers.cohort_attribution import ensure_schema
    from scripts.audit.weekly_bleed_report import build_report

    conn = _mk_conn()
    ensure_schema(conn)
    _seed_evaluated_opportunities_table(conn)
    _seed_rejected_opportunities_table(conn)

    _seed_cohort_row(
        conn, asset="ETH", strategy="MAKER_PATIENT", price_band_5c=15,
        cohort_date="2026-05-18", n_30d=70, cal_gap_30d=0.10,
        mean_cal_prob_30d=0.95, wr_30d=0.85,
    )
    _seed_cohort_row(
        conn, asset="SOL", strategy="MAKER_PATIENT", price_band_5c=16,
        cohort_date="2026-05-18", n_30d=80, cal_gap_30d=-0.18,
        mean_cal_prob_30d=0.77, wr_30d=0.95,
    )
    _seed_cohort_row(
        conn, asset="BTC", strategy="TAKER_NOW", price_band_5c=18,
        cohort_date="2026-05-18", n_30d=60, cal_gap_30d=0.05,
        mean_cal_prob_30d=0.95, wr_30d=0.90,
    )

    md = build_report(conn, report_date="2026-05-18")

    cal_section = md.split("## 2.")[1].split("## 3.")[0]
    sol_pos = cal_section.find("SOL")
    eth_pos = cal_section.find("ETH")
    btc_pos = cal_section.find("BTC")
    assert sol_pos != -1 and eth_pos != -1 and btc_pos != -1
    assert sol_pos < eth_pos < btc_pos, (
        "Order by |cal_gap| DESC: SOL (-0.18) > ETH (0.10) > BTC (0.05)"
    )


# ─── Pin 4 — cohort_date regex validation ────────────────────────────────────


def test_validate_report_date_rejects_malformed_input():
    """Path-injection safety: the cohort_date string flows into a file
    path (`weekly-bleed-{date}.md`), so it MUST validate against the
    strict `^\\d{4}-\\d{2}-\\d{2}$` regex before any filesystem write.
    A regression that dropped the validator would let `../../etc/passwd`
    escape the kb/findings/ directory."""
    from scripts.audit.weekly_bleed_report import validate_report_date

    assert validate_report_date("2026-05-18") == "2026-05-18"
    assert validate_report_date("2026-01-01") == "2026-01-01"

    for bad in (
        "2026-5-18",      # missing leading zero
        "26-05-18",       # 2-digit year
        "2026/05/18",     # wrong delimiter
        "2026-05-18 ",    # trailing whitespace
        " 2026-05-18",    # leading whitespace
        "../etc/passwd",  # path traversal attempt
        "2026-13-01",     # invalid month (parser must reject)
        "2026-05-32",     # invalid day
        "",               # empty
    ):
        with pytest.raises(ValueError):
            validate_report_date(bad)


# ─── Pin 5 — coverage-stats numerator/denominator math ───────────────────────


def test_coverage_stats_section_counts_match_underlying_tables():
    """Section 5 reports the actual rowcounts from
    `evaluated_opportunities` (broken down by candidate vs cell-block)
    plus `rejected_opportunities` over the report-week window. A
    regression that swapped the COUNT predicate would silently
    under-/over-report bleed-cell coverage to the operator."""
    from bot.helpers.cohort_attribution import ensure_schema
    from scripts.audit.weekly_bleed_report import build_report

    conn = _mk_conn()
    ensure_schema(conn)
    _seed_evaluated_opportunities_table(conn)
    _seed_rejected_opportunities_table(conn)

    # report_date 2026-05-18, week window 2026-05-12 → 2026-05-18
    week_inside = "2026-05-15T12:00:00+00:00"
    week_outside_old = "2026-05-05T12:00:00+00:00"

    # 3 candidate admits + 2 cell-block admits + 7 rejects within window
    for _ in range(3):
        _seed_admit(conn, filter_stage="candidate", eval_time=week_inside)
    for _ in range(2):
        _seed_admit(conn, filter_stage="SOL_BLEED_V2_88_93C_2_5MIN", eval_time=week_inside)
    for _ in range(7):
        _seed_reject(conn, eval_time=week_inside)
    # Outside window — must NOT be counted
    _seed_admit(conn, filter_stage="candidate", eval_time=week_outside_old)
    _seed_reject(conn, eval_time=week_outside_old)

    _seed_cohort_row(conn, cohort_date="2026-05-18")

    md = build_report(conn, report_date="2026-05-18")
    coverage = md.split("## 5.")[1]

    assert "candidate admits" in coverage.lower()
    # Counts present in the section
    assert "3" in coverage  # candidate
    assert "2" in coverage  # cell-block
    assert "7" in coverage  # rejected


# ─── Pin 6 — idempotent overwrite ────────────────────────────────────────────


def test_generate_weekly_report_idempotent_on_same_date(tmp_path):
    """Running the generator twice on the same report_date MUST produce
    byte-identical output (modulo whatever timestamp normalization the
    impl chooses). The script overwrites `kb/findings/weekly-bleed-
    {date}.md` cleanly."""
    from bot.helpers.cohort_attribution import ensure_schema
    from scripts.audit.weekly_bleed_report import generate_weekly_report

    conn = _mk_conn()
    ensure_schema(conn)
    _seed_evaluated_opportunities_table(conn)
    _seed_rejected_opportunities_table(conn)
    _seed_cohort_row(
        conn, asset="SOL", cohort_date="2026-05-18",
        cf_pnl_30d_dollars=-100.0, sum_cf_cents_30d=-10000,
    )

    # Pin generated timestamp so the "modulo timestamp" caveat is moot.
    fixed_now = _dt.datetime(2026, 5, 18, 13, 13, 0, tzinfo=_dt.timezone.utc)

    p1 = generate_weekly_report(
        conn, report_date="2026-05-18", output_dir=tmp_path, now=fixed_now,
    )
    body1 = p1.read_bytes()

    p2 = generate_weekly_report(
        conn, report_date="2026-05-18", output_dir=tmp_path, now=fixed_now,
    )
    body2 = p2.read_bytes()

    assert p1 == p2
    assert body1 == body2, "Idempotent overwrite must be byte-identical"


# ─── Pin 7 — R7 advisory stale-snapshot fallback ─────────────────────────────


def test_stale_snapshot_surfaces_stale_marker():
    """R7 advisory pass-forward (cohort design § P1.4): when the latest
    `cohort_date` is older than ~36h before the report_date, the report
    surfaces `Stale: True` rather than silently rendering a 3-day-old
    snapshot as if it were fresh. Empty-table case also stale."""
    from bot.helpers.cohort_attribution import ensure_schema
    from scripts.audit.weekly_bleed_report import build_report

    # Case A: empty table → stale True
    conn = _mk_conn()
    ensure_schema(conn)
    _seed_evaluated_opportunities_table(conn)
    _seed_rejected_opportunities_table(conn)
    md = build_report(conn, report_date="2026-05-18")
    assert "**Stale**: True" in md or "**Stale**: true" in md.lower()

    # Case B: latest row is 3 days stale (report_date - 3d)
    conn2 = _mk_conn()
    ensure_schema(conn2)
    _seed_evaluated_opportunities_table(conn2)
    _seed_rejected_opportunities_table(conn2)
    _seed_cohort_row(conn2, cohort_date="2026-05-15")  # 3d before 2026-05-18
    md2 = build_report(conn2, report_date="2026-05-18")
    assert "**Stale**: True" in md2

    # Case C: fresh row from the report_date itself → NOT stale
    conn3 = _mk_conn()
    ensure_schema(conn3)
    _seed_evaluated_opportunities_table(conn3)
    _seed_rejected_opportunities_table(conn3)
    _seed_cohort_row(conn3, cohort_date="2026-05-18")
    md3 = build_report(conn3, report_date="2026-05-18")
    assert "**Stale**: False" in md3


# ─── Pin 8 — section 3 new-fires within week window ──────────────────────────


def test_new_alerts_section_filters_to_week_window():
    """Section 3 includes cohorts whose FIRST firing date falls within
    [report_date - 7d, report_date]. Cohorts that first fired before
    the window MUST NOT appear (those are "old fires", surfaced
    elsewhere)."""
    from bot.helpers.cohort_attribution import ensure_schema
    from scripts.audit.weekly_bleed_report import build_report

    conn = _mk_conn()
    ensure_schema(conn)
    _seed_evaluated_opportunities_table(conn)
    _seed_rejected_opportunities_table(conn)

    # Cohort A: first-fire INSIDE window (2026-05-15)
    _seed_cohort_row(
        conn, asset="SOL", strategy="MAKER_PATIENT", price_band_5c=16,
        cohort_date="2026-05-15", alert_state="firing_bleed",
        last_alert_time="2026-05-15T13:07:00+00:00",
        cf_pnl_30d_dollars=-200.0, sum_cf_cents_30d=-20000,
        n_30d=80, wilson95_hi_30d=0.88,
    )
    _seed_cohort_row(
        conn, asset="SOL", strategy="MAKER_PATIENT", price_band_5c=16,
        cohort_date="2026-05-18", alert_state="firing_bleed",
        last_alert_time="2026-05-15T13:07:00+00:00",
        cf_pnl_30d_dollars=-210.0, sum_cf_cents_30d=-21000,
        n_30d=85, wilson95_hi_30d=0.87,
    )

    # Cohort B: first-fire OUTSIDE window (2026-05-08, > 7d before 05-18)
    _seed_cohort_row(
        conn, asset="ETH", strategy="MAKER_PATIENT", price_band_5c=15,
        cohort_date="2026-05-08", alert_state="firing_bleed",
        last_alert_time="2026-05-08T13:07:00+00:00",
        cf_pnl_30d_dollars=-150.0, sum_cf_cents_30d=-15000,
        n_30d=70, wilson95_hi_30d=0.89,
    )
    _seed_cohort_row(
        conn, asset="ETH", strategy="MAKER_PATIENT", price_band_5c=15,
        cohort_date="2026-05-18", alert_state="firing_bleed",
        last_alert_time="2026-05-08T13:07:00+00:00",
        cf_pnl_30d_dollars=-180.0, sum_cf_cents_30d=-18000,
        n_30d=75, wilson95_hi_30d=0.88,
    )

    md = build_report(conn, report_date="2026-05-18")
    section3 = md.split("## 3.")[1].split("## 4.")[0]
    assert "SOL" in section3, (
        "SOL first-fired 2026-05-15 (inside week) must appear in section 3"
    )
    assert "ETH" not in section3, (
        "ETH first-fired 2026-05-08 (>7d before report) must NOT appear "
        "in section 3"
    )


# ─── Pin 9 — section 4 exits read last_alert_time ────────────────────────────


def test_exited_alerts_section_includes_resolution_info():
    """Section 4 lists cohorts whose alert_state transitioned from
    'firing_*' (within the prior week) to 'quiet' (on report_date),
    surfacing the preserved last_alert_time so the operator can
    correlate with what shipped to resolve it."""
    from bot.helpers.cohort_attribution import ensure_schema
    from scripts.audit.weekly_bleed_report import build_report

    conn = _mk_conn()
    ensure_schema(conn)
    _seed_evaluated_opportunities_table(conn)
    _seed_rejected_opportunities_table(conn)

    # Cohort that fired on 2026-05-13 and resolved on 2026-05-18
    _seed_cohort_row(
        conn, asset="XRP", strategy="MAKER_PATIENT", price_band_5c=18,
        cohort_date="2026-05-13", alert_state="firing_bleed",
        last_alert_time="2026-05-13T13:07:00+00:00",
        cf_pnl_30d_dollars=-90.0, sum_cf_cents_30d=-9000,
    )
    _seed_cohort_row(
        conn, asset="XRP", strategy="MAKER_PATIENT", price_band_5c=18,
        cohort_date="2026-05-18", alert_state="quiet",
        last_alert_time="2026-05-13T13:07:00+00:00",  # preserved per design
        cf_pnl_30d_dollars=-20.0, sum_cf_cents_30d=-2000,
    )

    md = build_report(conn, report_date="2026-05-18")
    section4 = md.split("## 4.")[1].split("## 5.")[0]
    assert "XRP" in section4
    assert "2026-05-13" in section4, (
        "last_alert_time of the resolved cohort must be surfaced for "
        "operator resolution-date attribution"
    )


# ─── Pin 10 — Telegram one-liner with summary counts ─────────────────────────


def test_generate_weekly_report_invokes_telegram_with_summary(tmp_path):
    """On success the generator sends a one-line summary via the
    injected telegram_send callable. Format mirrors design § Weekly
    bleed report § Delivery:
    "📊 Weekly bleed report ready: N firing, M new, K resolved. <link>"
    """
    from bot.helpers.cohort_attribution import ensure_schema
    from scripts.audit.weekly_bleed_report import generate_weekly_report

    conn = _mk_conn()
    ensure_schema(conn)
    _seed_evaluated_opportunities_table(conn)
    _seed_rejected_opportunities_table(conn)
    # 1 firing on 2026-05-18, 1 new this week (first-fired 2026-05-15)
    _seed_cohort_row(
        conn, asset="SOL", strategy="MAKER_PATIENT", price_band_5c=16,
        cohort_date="2026-05-15", alert_state="firing_bleed",
        last_alert_time="2026-05-15T13:07:00+00:00",
        cf_pnl_30d_dollars=-200.0, sum_cf_cents_30d=-20000,
    )
    _seed_cohort_row(
        conn, asset="SOL", strategy="MAKER_PATIENT", price_band_5c=16,
        cohort_date="2026-05-18", alert_state="firing_bleed",
        last_alert_time="2026-05-15T13:07:00+00:00",
        cf_pnl_30d_dollars=-200.0, sum_cf_cents_30d=-20000,
    )

    sent: List[str] = []
    def fake_send(msg: str) -> bool:
        sent.append(msg)
        return True

    generate_weekly_report(
        conn, report_date="2026-05-18", output_dir=tmp_path,
        telegram_send=fake_send,
    )

    assert len(sent) == 1, "Telegram one-liner must fire exactly once"
    payload = sent[0]
    assert "Weekly bleed report" in payload
    assert "firing" in payload
    assert "new" in payload
    assert "resolved" in payload


# ─── Pin 11 — sections cap at TOP_N ──────────────────────────────────────────


# ─── Pin 12 — section 4 boundary (R1 MN1 fix) ────────────────────────────────


def test_exited_alerts_includes_transition_on_first_day_of_week():
    """R1 MN1 boundary case: a cohort whose last firing was on the
    firing-window LOWER bound (`report_date - 7d` = 2026-05-11) and is
    quiet on `report_date` (2026-05-18) MUST appear in section 4.

    Before the fix, the firing-window predicate `[week_start, week_end]`
    = `[report_date - 6d, report_date]` excluded firings on
    `report_date - 7d`, so the longest-resolved-this-week edge was
    invisible. The fix aligns the firing window to
    `[report_date - 7d, report_date - 1d]` so resolutions on ANY day of
    the report week are caught — including the boundary case where the
    transition happened on `week_start`.
    """
    from bot.helpers.cohort_attribution import ensure_schema
    from scripts.audit.weekly_bleed_report import build_report

    conn = _mk_conn()
    ensure_schema(conn)
    _seed_evaluated_opportunities_table(conn)
    _seed_rejected_opportunities_table(conn)

    # report_date 2026-05-18, week_start = 2026-05-12 (report_date - 6d).
    # Cohort fired 2026-05-11 (week_start - 1d) → transitioned 2026-05-12.
    _seed_cohort_row(
        conn, asset="DOGE", strategy="MAKER_PATIENT", price_band_5c=16,
        cohort_date="2026-05-11", alert_state="firing_bleed",
        last_alert_time="2026-05-11T13:07:00+00:00",
        cf_pnl_30d_dollars=-100.0, sum_cf_cents_30d=-10000,
    )
    _seed_cohort_row(
        conn, asset="DOGE", strategy="MAKER_PATIENT", price_band_5c=16,
        cohort_date="2026-05-18", alert_state="quiet",
        last_alert_time="2026-05-11T13:07:00+00:00",
        cf_pnl_30d_dollars=-10.0, sum_cf_cents_30d=-1000,
    )

    md = build_report(conn, report_date="2026-05-18")
    section4 = md.split("## 4.")[1].split("## 5.")[0]
    assert "DOGE" in section4, (
        "Cohort firing on week_start - 1d and resolving within week must "
        "surface in section 4 (R1 MN1 boundary case)"
    )
    assert "2026-05-11" in section4, (
        "last_fire_date must render even when it falls 1 day before week_start"
    )


def test_bleeders_and_cal_drift_sections_cap_at_10():
    """Section 1 and Section 2 each cap at 10 rows so the operator-
    facing report doesn't sprawl. Mirrors P1.2 dashboard panel's
    20-row cap (smaller here because the weekly report is meant to
    be skimmed in one glance)."""
    from bot.helpers.cohort_attribution import ensure_schema
    from scripts.audit.weekly_bleed_report import (
        BLEED_TOP_N,
        CAL_DRIFT_TOP_N,
        build_report,
    )

    assert BLEED_TOP_N == 10
    assert CAL_DRIFT_TOP_N == 10

    conn = _mk_conn()
    ensure_schema(conn)
    _seed_evaluated_opportunities_table(conn)
    _seed_rejected_opportunities_table(conn)

    for i in range(15):
        _seed_cohort_row(
            conn, asset="SOL", strategy="MAKER_PATIENT",
            price_band_5c=i + 1, cohort_date="2026-05-18",
            cf_pnl_30d_dollars=-100.0 - i,
            sum_cf_cents_30d=-(10000 + i * 100),
            cal_gap_30d=0.10 + i * 0.001,
        )

    md = build_report(conn, report_date="2026-05-18")
    bleeders_section = md.split("## 1.")[1].split("## 2.")[0]
    cal_section = md.split("## 2.")[1].split("## 3.")[0]

    # Count table data rows (lines starting with "| SOL ")
    bleeder_rows = [ln for ln in bleeders_section.splitlines() if ln.startswith("| SOL ")]
    cal_rows = [ln for ln in cal_section.splitlines() if ln.startswith("| SOL ")]

    assert len(bleeder_rows) == 10
    assert len(cal_rows) == 10
