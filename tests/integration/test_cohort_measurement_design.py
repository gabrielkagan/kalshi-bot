"""Phase 1 (Money Printer Roadmap) cohort-measurement design pins.

Spike: ClickUp 86b9x2dxx — design doc kb/decisions/cohort-measurement-design-may12.md.

These tests pin the pure-function contracts (band classifiers + alert
triggers) that the 4 follow-up Bits implement:

- P1.1 `bot/helpers/cohort_attribution.py` (band classifiers)
- P1.3 `bot/helpers/cohort_alerts.py` (alert triggers)

Per L99 (deploy-gate-failure-from-tdd-red-tests): every test is marked
`@pytest.mark.xfail(strict=True, reason="Sister Bit P1.x pending")` so:

- Pre-sister-Bit (current state): the module imports raise ImportError →
  pytest records as xfail → deploy gate passes.
- Post-sister-Bit ship: the module exists, the assertion passes → xpass →
  strict-True flags the test as FAILED, forcing the implementer to remove
  the decorator and acknowledge the contract is now live.

The xfail-strict-True pattern itself mirrors the Wave 1 A.1a precedent
(5 strict-True'd xfails for sister Bit A.1b, per
`kb/decisions/session-resume-may12-from-wave-1-training-data-shipped.md`).
The file structure (helper imports, boundary-case pins) mirrors
`tests/integration/test_stc_structural_clustering.py` — that sister
spike is shipped + always-pass so does not itself use xfail; the
mirror is in test-shape, not in marker discipline.

**Disposition (per design doc § Discipline applied):** this file is
drafted in the design-spike but **does NOT ship in the spike's commit**.
It is consumed by Bit P1.1 (band classifiers) + Bit P1.3 (alert
triggers) — each lands the helpers AND removes the relevant xfail
decorators in the same commit. Until then, the file documents the
contract in code form for the next implementer.

This scaffold drafts 23 pins (6 P1.1 band-classifier/helper + 11 P1.3
alert-trigger + 2 alert-write-back transition + 4 P1.1 materialization-
contract pins added at the P1.1 ship for: 23-col PRAGMA schema check,
idempotent rerun (INSERT OR REPLACE on composite PK), honest-NULL on
missing market_result (cohort row excluded; NOT stamped with 0), and
brand-new-cohort handling (compute_next_alert_state with prior_row=None
returns ('quiet', None) per R7 advisory). 12 of the 23 are P1.1-owned
(decorators removed at this Bit's ship); 11 remain xfail-strict-True
pending P1.3.
"""
from __future__ import annotations

import datetime as _dt
import sqlite3

import pytest

# ─── Pillar-1: band classifier contracts (sister Bit P1.1 — ticket P1.1) ──────


def test_price_band_5c_floor_division():
    """price_band_5c = market_price_cents // 5.

    Boundary cases:
    - market_price=4 → band 0 (cents 0-4)
    - market_price=5 → band 1 (cents 5-9)
    - market_price=88 → band 17 (cents 85-89; matches SOL_TAKER_LOWPRICE band)
    - market_price=99 → band 19 (cents 95-99; near-cert TM98 band edge)
    """
    from bot.helpers.cohort_attribution import compute_price_band_5c

    assert compute_price_band_5c(4) == 0
    assert compute_price_band_5c(5) == 1
    assert compute_price_band_5c(88) == 17
    assert compute_price_band_5c(99) == 19


def test_stc_band_60s_inclusive_lower_exclusive_upper():
    """stc_band_60s uses lo ≤ stc < hi convention (mirrors stc-clustering spike).

    Boundary cases:
    - seconds_to_close=0.0 → band 0
    - seconds_to_close=59.9 → band 0
    - seconds_to_close=60.0 → band 1
    - seconds_to_close=300.0 → band 5 (NOT band 4; the SOL_BLEED_V2 121-300s
      predicate uses `stc <= 300`, but the band classifier uses `< 300`).
      The cohort `cell_block_stage` column carries the gate's own
      classification so this band-edge difference is benign for UNION
      reconstruction.
    """
    from bot.helpers.cohort_attribution import compute_stc_band_60s

    assert compute_stc_band_60s(0.0) == 0
    assert compute_stc_band_60s(59.9) == 0
    assert compute_stc_band_60s(60.0) == 1
    assert compute_stc_band_60s(300.0) == 5


def test_stc_band_60s_tail_at_600s_plus():
    """STC bands 0..10 (0-60..540-600) plus tail band 11 for >=600s.

    Stc-clustering spike used a literal '600+' string in the band column;
    P1.1 uses INTEGER 11 for compactness + index-friendliness.
    """
    from bot.helpers.cohort_attribution import compute_stc_band_60s

    assert compute_stc_band_60s(599.9) == 9
    assert compute_stc_band_60s(600.0) == 10  # 600 ≤ stc < 660 → band 10
    assert compute_stc_band_60s(660.0) == 11
    assert compute_stc_band_60s(3600.0) == 11  # all 600+ folds into tail


def test_cohort_partition_stages_canonical():
    """The cohort partition-stage set is the single source of truth for
    `filter_stage IN (...)` predicates in cohort aggregation.

    Includes the BASELINE 'candidate' rollup AND the 4 bleed-cell stages.
    Named `COHORT_PARTITION_STAGES` (not `BLEED_CELL_UNION_STAGES`)
    because `'candidate'` is not itself a bleed-cell — it is the
    pre-block rollup we UNION with the bleed-cell stages.

    The 3 sister-doc UNION lists at scripts/CLAUDE.md / bot/CLAUDE.md /
    bot/scanner/CLAUDE.md enumerate only 3 bleed-cells each and are
    stale post-V2 ship (2026-05-10) — missing SOL_BLEED_V2_88_93C_2_5MIN.
    A sister-doc patch is filed as follow-up to bring the 3 lists into
    agreement with this constant.

    Future cell-block additions MUST update this constant in P1.1 — that
    single update propagates to dashboard + alert + weekly report.
    """
    from bot.helpers.cohort_attribution import COHORT_PARTITION_STAGES

    expected = {
        "candidate",
        "96C_SOL_XRP_STC_DANGER_BAND",
        "TM98_97_98C_2_5MIN_BLEED",
        "SOL_TAKER_85_89C_2_5MIN_BLEED",
        "SOL_BLEED_V2_88_93C_2_5MIN",
    }
    assert COHORT_PARTITION_STAGES == expected


def test_cohort_key_tuple_canonical_ordering():
    """The cohort key is a 6-tuple in canonical (asset, product_type,
    strategy, price_band_5c, stc_band_60s, cell_block_stage) ordering.

    Pins against accidental dimension reordering on dashboard reads.
    """
    from bot.helpers.cohort_attribution import cohort_key_tuple

    row = {
        "asset": "SOL",
        "product_type": "15m",
        "strategy": "MAKER_PATIENT",
        "market_price": 90,
        "seconds_to_close": 280.0,
        "filter_stage": "candidate",
    }
    key = cohort_key_tuple(row)
    assert key == ("SOL", "15m", "MAKER_PATIENT", 18, 4, "candidate")


def test_wilson95_parity_with_inline_pattern():
    """Wilson95 CI parity check vs the inline implementation pattern
    used by `scripts/audit/stc_structural_clustering.py` (no
    `scripts/audit/wilson_ci.py` helper exists today; design intent is
    inline z=1.96 binomial-proportion, identical formula).

    Pinned reference values (k=10, n=100): Wilson95 ≈ [0.0552, 0.1744].
    Tolerance 2e-3 to absorb rounding-choice differences across
    implementations.
    """
    from bot.helpers.cohort_attribution import wilson95_ci

    lo, hi = wilson95_ci(k=10, n=100)
    assert abs(lo - 0.0552) < 2e-3
    assert abs(hi - 0.1744) < 2e-3


# ─── Pillar-3: alert trigger contracts (sister Bit P1.3 — ticket P1.3) ────────


def test_bleed_alert_fires_at_n50_wilson_hi_below_092_cf_below_minus50():
    """BLEED trigger fires when ALL: n_30d ≥ 50, wilson95_hi < 0.92,
    cf_pnl_30d_dollars < -50, cell_block_stage='candidate'.

    SOL × MAKER_PATIENT × 480-540s cohort (from stc-clustering spike)
    should fire: n=138, wilson95_hi=0.892, cf_pnl=-285.64, candidate.
    """
    from bot.helpers.cohort_alerts import should_fire_bleed_alert

    assert should_fire_bleed_alert(
        n=138, wilson95_hi=0.892, cf_pnl_30d_dollars=-285.64,
        cell_block_stage="candidate",
    ) is True


def test_bleed_alert_does_not_fire_on_already_blocked_cohort():
    """BLEED trigger does NOT fire on cell_block_stage != 'candidate'.

    Post-V2 SOL_BLEED_V2_88_93C_2_5MIN rows match all numeric criteria
    but the alert is silenced — the gate already covers them. Firing
    on an already-blocked cohort would be confusing operator noise.
    """
    from bot.helpers.cohort_alerts import should_fire_bleed_alert

    assert should_fire_bleed_alert(
        n=138, wilson95_hi=0.892, cf_pnl_30d_dollars=-285.64,
        cell_block_stage="SOL_BLEED_V2_88_93C_2_5MIN",
    ) is False


def test_bleed_alert_n_boundary_49_vs_50():
    """n=49 → no fire; n=50 → fire. Boundary pin against off-by-one drift."""
    from bot.helpers.cohort_alerts import should_fire_bleed_alert

    common = dict(wilson95_hi=0.89, cf_pnl_30d_dollars=-100.0, cell_block_stage="candidate")
    assert should_fire_bleed_alert(n=49, **common) is False
    assert should_fire_bleed_alert(n=50, **common) is True


def test_bleed_alert_wilson_hi_boundary_0919_vs_0920():
    """wilson95_hi=0.9199 → fire (strictly < 0.92); wilson95_hi=0.9200 → no fire."""
    from bot.helpers.cohort_alerts import should_fire_bleed_alert

    common = dict(n=100, cf_pnl_30d_dollars=-100.0, cell_block_stage="candidate")
    assert should_fire_bleed_alert(wilson95_hi=0.9199, **common) is True
    assert should_fire_bleed_alert(wilson95_hi=0.9200, **common) is False


def test_bleed_alert_cf_pnl_boundary_minus49_99_vs_minus50_01():
    """cf_pnl=-49.99 → no fire; cf_pnl=-50.01 → fire. Strictly < -50."""
    from bot.helpers.cohort_alerts import should_fire_bleed_alert

    common = dict(n=100, wilson95_hi=0.89, cell_block_stage="candidate")
    assert should_fire_bleed_alert(cf_pnl_30d_dollars=-49.99, **common) is False
    assert should_fire_bleed_alert(cf_pnl_30d_dollars=-50.01, **common) is True


def test_calibration_alert_requires_3_day_persistence():
    """CALIBRATION trigger requires persistence_days ≥ 3.

    Single-day spikes (persistence=1) do NOT fire — too noisy.
    persistence=2 still no, persistence=3 yes. Pins the v1 mandatory
    false-positive mitigation from the design doc.
    """
    from bot.helpers.cohort_alerts import should_fire_calibration_alert

    common = dict(n=100, abs_cal_gap=0.08)  # 8pp gap, n=100 — exceeds thresholds
    assert should_fire_calibration_alert(persistence_days=1, **common) is False
    assert should_fire_calibration_alert(persistence_days=2, **common) is False
    assert should_fire_calibration_alert(persistence_days=3, **common) is True


def test_calibration_alert_gap_boundary_0_0499_vs_0_0501():
    """abs_cal_gap=0.0499 → no fire; abs_cal_gap=0.0501 → fire (with
    n≥30 and persistence_days≥3). Strictly > 0.05."""
    from bot.helpers.cohort_alerts import should_fire_calibration_alert

    common = dict(n=30, persistence_days=3)
    assert should_fire_calibration_alert(abs_cal_gap=0.0499, **common) is False
    assert should_fire_calibration_alert(abs_cal_gap=0.0501, **common) is True


def test_cooldown_active_within_23h_blocks_realert():
    """23h cooldown per cohort: an alert that fired <23h ago does not
    re-fire on the next nightly run. 23h not 24h: nightly aggregator
    runs at 13:07 UTC with NTP/cron jitter; a strict 24h window can
    flip-suppress alerts day-over-day if today's cron fires a few
    seconds before yesterday's `last_alert_time + 24h`.

    cooldown_active(last_alert_time, now, hours=23) returns:
    - True if now - last_alert_time < 23h
    - False if last_alert_time is None or gap ≥ 23h
    """
    from datetime import datetime, timedelta

    from bot.helpers.cohort_alerts import cooldown_active

    now = datetime(2026, 5, 12, 13, 7, 0)
    just_fired = now - timedelta(hours=1)
    long_ago = now - timedelta(hours=24)  # well past 23h window

    assert cooldown_active(last_alert_time=just_fired, now=now) is True
    assert cooldown_active(last_alert_time=long_ago, now=now) is False
    assert cooldown_active(last_alert_time=None, now=now) is False


def test_cooldown_active_exact_23h_boundary():
    """Cooldown boundary: 22h59m → active; 23h00m00s → EXPIRED (>= boundary);
    23h00m01s → expired.

    Strict ≥23h to expire. Mirrors the BLEED n / cf_pnl / wilson_hi
    boundary-pinning style: each numeric threshold gets its own pin.
    """
    from datetime import datetime, timedelta

    from bot.helpers.cohort_alerts import cooldown_active

    now = datetime(2026, 5, 12, 13, 7, 0)
    just_under_23h = now - timedelta(hours=22, minutes=59)
    exactly_23h = now - timedelta(hours=23)
    just_over_23h = now - timedelta(hours=23, seconds=1)

    assert cooldown_active(last_alert_time=just_under_23h, now=now) is True
    assert cooldown_active(last_alert_time=exactly_23h, now=now) is False
    assert cooldown_active(last_alert_time=just_over_23h, now=now) is False


def test_calibration_alert_n_boundary_29_vs_30():
    """CAL n boundary: n=29 → no fire; n=30 → fire (with persistence + gap).

    Mirrors BLEED n=49 vs 50 pattern. Strict ≥30.
    """
    from bot.helpers.cohort_alerts import should_fire_calibration_alert

    common = dict(abs_cal_gap=0.08, persistence_days=3)
    assert should_fire_calibration_alert(n=29, **common) is False
    assert should_fire_calibration_alert(n=30, **common) is True


def test_calibration_alert_negative_gap_symmetric_abs():
    """CAL trigger uses abs() — cal_gap=−0.06 fires identically to +0.06.

    The point estimate `cal_gap = mean_cal_prob − realized_wr` can be
    negative (cohort under-rates and over-realizes — still a calibrator
    drift). The trigger must be sign-blind: `abs(cal_gap) > 0.05`.
    """
    from bot.helpers.cohort_alerts import should_fire_calibration_alert

    common = dict(n=100, persistence_days=3)
    # Positive over-realization (calibrator too pessimistic)
    assert should_fire_calibration_alert(abs_cal_gap=0.06, **common) is True
    # The helper takes ABS gap as input; design contract is that the
    # CALLER (nightly aggregator) computes abs(cal_gap) before passing.
    # Pin both signs by calling abs(...) at the test boundary, mirroring
    # what the production caller does.
    assert should_fire_calibration_alert(abs_cal_gap=abs(-0.06), **common) is True
    # Below threshold either sign
    assert should_fire_calibration_alert(abs_cal_gap=abs(-0.04), **common) is False


# ─── Pillar-4: alert state write-back (sister Bit P1.1 — graceful fallback) ────


def test_aggregator_graceful_fallback_when_cohort_alerts_module_missing():
    """When `bot.helpers.cohort_alerts` is absent (P1.1 ships before
    P1.3), the nightly aggregator must NOT propagate ImportError. It
    writes `alert_state='quiet'` + `last_alert_time=NULL` for every
    cohort row and commits successfully.

    Pinned at the helper-export level: P1.1's aggregator module must
    expose a `run_aggregation(conn, *, alerts_module=None)` (or
    equivalent) where `alerts_module=None` triggers the fallback path.
    The test stubs `alerts_module=None` to simulate the missing-import
    state without monkeypatching sys.modules.

    Fixture: seed 1 settled candidate row so the aggregator actually
    materializes at least one cohort row to test against (R1 M4 —
    without a seed the for-loop is vacuously satisfied).
    """
    from bot.helpers.cohort_attribution import run_aggregation

    conn = sqlite3.connect(":memory:")
    _eval_opp_schema_min(conn)
    now_iso = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=1)).isoformat()
    _insert_eval_row(
        conn,
        ticker="KX1", asset="SOL", product_type="15m",
        strategy="MAKER_PATIENT", filter_stage="candidate",
        market_price=88, seconds_to_close=250.0,
        market_result="yes", counterfactual_pnl=1500,
        calibrated_prob=0.92, evaluation_time=now_iso,
    )
    conn.commit()

    run_aggregation(conn, alerts_module=None)  # must NOT raise
    rows = conn.execute(
        "SELECT alert_state, last_alert_time FROM cohort_attribution_daily"
    ).fetchall()
    assert len(rows) >= 1
    for state, last in rows:
        assert state == "quiet"
        assert last is None


def test_firing_to_quiet_transition_preserves_last_alert_time():
    """When a cohort previously had `alert_state='firing_bleed'` and the
    current `cohort_date` no longer meets BLEED criteria, the new
    aggregation row stamps `alert_state='quiet'` while `last_alert_time`
    is **preserved unchanged**.

    This contract is load-bearing for P1.4's weekly bleed report § 4
    ("Cohorts that EXITED alert state this week — what fixed them"):
    the resolution-date attribution needs the original fire-time
    preserved on the post-transition row.
    """
    from bot.helpers.cohort_attribution import compute_next_alert_state

    prior_row = {
        "alert_state": "firing_bleed",
        "last_alert_time": "2026-05-10T13:07:00Z",
    }
    current_metrics = {
        "n_30d": 60,
        "wilson95_hi_30d": 0.95,  # above 0.92 floor — does NOT fire BLEED
        "cf_pnl_30d_dollars": +5.0,  # positive — does NOT fire BLEED
        "abs_cal_gap_30d": 0.01,
        "persistence_days": 1,
    }
    next_state, next_last_alert = compute_next_alert_state(prior_row, current_metrics)
    assert next_state == "quiet"
    assert next_last_alert == "2026-05-10T13:07:00Z"  # preserved


# ─── P1.1 materialization-contract pins (added at this Bit's ship) ────────────
# These pins land WITHOUT @pytest.mark.xfail because the P1.1 helpers + table
# DDL ship in the same commit. They're RED before the implementation lands
# (ImportError on bot.helpers.cohort_attribution) and GREEN after — the
# classic TDD-first cycle. The 8 P1.1 xfail decorators above similarly
# flip xpass → strict-True FAIL after the helpers exist and are removed at
# this commit.


def _eval_opp_schema_min(conn: sqlite3.Connection) -> None:
    """Create the minimal `evaluated_opportunities` subset the aggregator
    reads. The production table has 146 cols; the aggregator only needs
    the partition keys + market_result + counterfactual_pnl + calibrated_prob
    + evaluation_time. Keeping the test fixture small avoids drift against
    the full production schema."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS evaluated_opportunities (
            ticker TEXT, asset TEXT, product_type TEXT, strategy TEXT,
            filter_stage TEXT, market_price INTEGER, seconds_to_close REAL,
            market_result TEXT, counterfactual_pnl INTEGER,
            calibrated_prob REAL, evaluation_time TEXT
        )""")


def _insert_eval_row(conn, **row):
    """Insert a row with the synthetic minimal schema. Caller passes
    keyword args matching the 11 cols above. NULL passed through."""
    cols = ("ticker", "asset", "product_type", "strategy", "filter_stage",
            "market_price", "seconds_to_close", "market_result",
            "counterfactual_pnl", "calibrated_prob", "evaluation_time")
    values = tuple(row.get(c) for c in cols)
    conn.execute(
        f"INSERT INTO evaluated_opportunities ({','.join(cols)}) "
        f"VALUES ({','.join('?' for _ in cols)})",
        values,
    )


def test_schema_pragma_verify_23_cols():
    """`cohort_attribution_daily` has exactly the 23 columns enumerated
    in the design doc § Storage CREATE TABLE block. PRAGMA table_info
    against the materialized table is the canonical contract.

    Composite PK on (cohort_date, asset, product_type, strategy,
    price_band_5c, stc_band_60s, cell_block_stage) is asserted separately
    by the idempotency pin (INSERT OR REPLACE on PK is the behavioral
    contract that uses the PK declaration).
    """
    from bot.helpers.cohort_attribution import ensure_schema

    conn = sqlite3.connect(":memory:")
    ensure_schema(conn)
    info = conn.execute("PRAGMA table_info(cohort_attribution_daily)").fetchall()
    cols = {row[1] for row in info}
    expected = {
        "cohort_date", "asset", "product_type", "strategy",
        "price_band_5c", "stc_band_60s", "cell_block_stage",
        "n_30d", "n_yes_30d", "n_no_30d", "wr_30d",
        "wilson95_lo_30d", "wilson95_hi_30d",
        "sum_cf_cents_30d", "cf_pnl_30d_dollars",
        "mean_cal_prob_30d", "cal_gap_30d",
        "n_7d", "wr_7d", "cf_pnl_7d_dollars", "cal_gap_7d",
        "alert_state", "last_alert_time",
    }
    assert cols == expected
    assert len(info) == 23


def test_idempotent_rerun():
    """Two runs of `run_aggregation` against the same evaluated_opportunities
    table produce identical row counts in `cohort_attribution_daily`.

    INSERT OR REPLACE on the composite PK is the load-bearing mechanism;
    the test confirms it works at the row-count level (PK-collision UPDATE,
    not DUPLICATE). The aggregator stamps each cohort row with the same
    `cohort_date` on both runs, so the PK collides and the row is replaced.

    Both runs pin `cohort_date='2026-05-12'` explicitly to immunize against
    UTC-midnight flakiness (R1 m4).
    """
    from bot.helpers.cohort_attribution import run_aggregation

    conn = sqlite3.connect(":memory:")
    _eval_opp_schema_min(conn)
    eval_time = "2026-05-11T13:07:00+00:00"  # within 30d of pinned cohort_date
    _insert_eval_row(
        conn,
        ticker="KXSOL15M-X", asset="SOL", product_type="15m",
        strategy="MAKER_PATIENT", filter_stage="candidate",
        market_price=88, seconds_to_close=250.0,
        market_result="yes", counterfactual_pnl=1500,
        calibrated_prob=0.92, evaluation_time=eval_time,
    )
    conn.commit()

    pinned_now = _dt.datetime(2026, 5, 12, 13, 7, tzinfo=_dt.timezone.utc)
    run_aggregation(conn, alerts_module=None, cohort_date="2026-05-12", now=pinned_now)
    n1 = conn.execute(
        "SELECT COUNT(*) FROM cohort_attribution_daily"
    ).fetchone()[0]
    assert n1 >= 1

    run_aggregation(conn, alerts_module=None, cohort_date="2026-05-12", now=pinned_now)
    n2 = conn.execute(
        "SELECT COUNT(*) FROM cohort_attribution_daily"
    ).fetchone()[0]
    assert n1 == n2


def test_honest_null_on_missing_market_result():
    """A row with `market_result IS NULL` is excluded entirely from the
    cohort aggregation — NOT stamped with wr=0 / cf_pnl=0.

    This is the honest-NULL contract: an un-settled (or yet-to-settle)
    row is forensic-poisoning if folded into wr/cf_pnl as a 0-outcome.
    Mirrors the existing pattern in `scripts/audit/stc_structural_clustering.py`
    (filter `market_result IN ('yes','no')` at the SQL level).
    """
    from bot.helpers.cohort_attribution import run_aggregation

    conn = sqlite3.connect(":memory:")
    _eval_opp_schema_min(conn)
    now_iso = _dt.datetime.now(_dt.timezone.utc).isoformat()
    # One settled row + one un-settled row in the same cohort cell.
    _insert_eval_row(
        conn,
        ticker="KXSOL15M-A", asset="SOL", product_type="15m",
        strategy="MAKER_PATIENT", filter_stage="candidate",
        market_price=88, seconds_to_close=250.0,
        market_result="yes", counterfactual_pnl=1500,
        calibrated_prob=0.92, evaluation_time=now_iso,
    )
    _insert_eval_row(
        conn,
        ticker="KXSOL15M-B", asset="SOL", product_type="15m",
        strategy="MAKER_PATIENT", filter_stage="candidate",
        market_price=88, seconds_to_close=250.0,
        market_result=None, counterfactual_pnl=None,
        calibrated_prob=0.92, evaluation_time=now_iso,
    )
    conn.commit()

    run_aggregation(conn, alerts_module=None)
    rows = conn.execute(
        "SELECT n_30d, n_yes_30d, n_no_30d FROM cohort_attribution_daily"
    ).fetchall()
    # Cohort row exists for the settled row only — n=1 (NOT n=2 with one
    # spurious 0-outcome). NULL row is filtered at the SQL level.
    assert len(rows) == 1
    n_30d, n_yes_30d, n_no_30d = rows[0]
    assert n_30d == 1
    assert n_yes_30d == 1
    assert n_no_30d == 0


def test_compute_next_alert_state_handles_brand_new_cohort():
    """R7 advisory pass-forward from the design spike: when `prior_row`
    is None (brand-new cohort with no prior aggregation entry),
    `compute_next_alert_state` returns ('quiet', None).

    A brand-new cohort cannot be "firing" until P1.3's trigger evaluates
    against its first metrics — the brand-new state is 'quiet' and the
    `last_alert_time` slot is empty. Pinning this explicitly closes the
    "design doc doesn't specify None prior_row" gap the R7 reviewer
    flagged.
    """
    from bot.helpers.cohort_attribution import compute_next_alert_state

    current_metrics = {
        "n_30d": 0,
        "wilson95_hi_30d": None,
        "cf_pnl_30d_dollars": None,
        "abs_cal_gap_30d": None,
        "persistence_days": 0,
    }
    next_state, next_last_alert = compute_next_alert_state(None, current_metrics)
    assert next_state == "quiet"
    assert next_last_alert is None


class _FakeAlertsModule:
    """In-process stub of `bot.helpers.cohort_alerts` (sister Bit P1.3).

    Lets us exercise the `alerts_module`-PRESENT branch of
    `compute_next_alert_state` before P1.3 lands. Predetermined returns
    make the integration testable in isolation from the sister Bit's
    actual threshold logic (which has its own xfail-strict-True pins
    pending its ship).
    """

    def __init__(self, *, fire_bleed=False, fire_cal=False, cooldown=False):
        self._fire_bleed = fire_bleed
        self._fire_cal = fire_cal
        self._cooldown = cooldown

    def should_fire_bleed_alert(self, **_kw):
        return self._fire_bleed

    def should_fire_calibration_alert(self, **_kw):
        return self._fire_cal

    def cooldown_active(self, **_kw):
        return self._cooldown


def test_compute_next_alert_state_transitions_quiet_to_firing_bleed_when_module_present():
    """When `alerts_module.should_fire_bleed_alert` returns True and no
    cooldown is active, the transition is quiet → firing_bleed and
    `last_alert_time` is set to `now` (R1 M3 pin — exercises the
    alerts_module-PRESENT branch of compute_next_alert_state)."""
    from bot.helpers.cohort_attribution import compute_next_alert_state

    prior_row = {"alert_state": "quiet", "last_alert_time": None}
    current_metrics = {
        "n_30d": 138,
        "wilson95_hi_30d": 0.892,
        "cf_pnl_30d_dollars": -285.64,
        "abs_cal_gap_30d": 0.02,
        "persistence_days": 1,
        "cell_block_stage": "candidate",
    }
    stub = _FakeAlertsModule(fire_bleed=True, fire_cal=False, cooldown=False)
    pinned_now = _dt.datetime(2026, 5, 12, 13, 7, tzinfo=_dt.timezone.utc)
    next_state, next_last_alert = compute_next_alert_state(
        prior_row, current_metrics, alerts_module=stub, now=pinned_now,
    )
    assert next_state == "firing_bleed"
    assert next_last_alert == pinned_now.isoformat()


def test_compute_next_alert_state_in_cooldown_preserves_prior_last_alert_time():
    """When the cohort would fire but `alerts_module.cooldown_active`
    returns True, the transition stays at `firing_bleed` (still in alert
    state) but `last_alert_time` is NOT updated to `now` — it preserves
    the prior fire-time so the 23h cooldown window stays anchored to the
    original first-fire moment (R1 M3 pin — covers the cooldown branch
    of the alerts_module-PRESENT path)."""
    from bot.helpers.cohort_attribution import compute_next_alert_state

    prior_fire_iso = "2026-05-12T01:00:00+00:00"
    prior_row = {"alert_state": "firing_bleed", "last_alert_time": prior_fire_iso}
    current_metrics = {
        "n_30d": 138,
        "wilson95_hi_30d": 0.892,
        "cf_pnl_30d_dollars": -285.64,
        "abs_cal_gap_30d": 0.02,
        "persistence_days": 1,
        "cell_block_stage": "candidate",
    }
    stub = _FakeAlertsModule(fire_bleed=True, fire_cal=False, cooldown=True)
    pinned_now = _dt.datetime(2026, 5, 12, 13, 7, tzinfo=_dt.timezone.utc)
    next_state, next_last_alert = compute_next_alert_state(
        prior_row, current_metrics, alerts_module=stub, now=pinned_now,
    )
    assert next_state == "firing_bleed"
    assert next_last_alert == prior_fire_iso  # NOT updated to pinned_now


# ─── P1.3 additions: payload formatters + bundled followups ──────────────────


def test_bleed_payload_contains_cohort_identity_and_metrics():
    """BLEED Telegram payload includes asset, strategy, price/STC bands,
    stage, n, wr, wilson_hi, cf_30d. Mirrors the design doc § Telegram
    payload format sample."""
    from bot.helpers.cohort_alerts import format_bleed_payload

    row = {
        "asset": "SOL", "product_type": "15m", "strategy": "MAKER_PATIENT",
        "price_band_5c": 17, "stc_band_60s": 8, "cell_block_stage": "candidate",
        "n_30d": 138, "wr_30d": 0.841, "wilson95_hi_30d": 0.892,
        "cf_pnl_30d_dollars": -285.64, "cohort_date": "2026-05-12",
    }
    payload = format_bleed_payload(row)
    assert "BLEED" in payload
    assert "SOL" in payload
    assert "MAKER_PATIENT" in payload
    assert "85-89" in payload  # price band 17 → 85-89¢
    assert "480-540" in payload  # stc band 8 → 480-540s
    assert "candidate" in payload
    assert "n=138" in payload
    assert "0.841" in payload
    assert "0.892" in payload
    assert "-285.64" in payload


def test_calibration_payload_contains_cal_drift_and_persistence():
    """CALIBRATION Telegram payload includes cal_prob, realized_wr, gap%
    and persistence_days. Sign of gap is displayed (+/−)."""
    from bot.helpers.cohort_alerts import format_calibration_payload

    row = {
        "asset": "ETH", "product_type": "15m", "strategy": "MAKER_PATIENT",
        "price_band_5c": 15, "stc_band_60s": 9, "cell_block_stage": "candidate",
        "n_30d": 70, "wr_30d": 0.843, "mean_cal_prob_30d": 0.912,
        "cal_gap_30d": 0.069, "persistence_days": 4, "cohort_date": "2026-05-12",
    }
    payload = format_calibration_payload(row)
    assert "CAL" in payload
    assert "ETH" in payload
    assert "MAKER_PATIENT" in payload
    assert "0.912" in payload
    assert "0.843" in payload
    assert "+6.9pp" in payload
    assert "4 days" in payload


def test_calibration_payload_renders_negative_gap_with_minus_sign():
    """Negative cal_gap (over-realization — calibrator under-rates) renders
    with a leading minus, not '+'."""
    from bot.helpers.cohort_alerts import format_calibration_payload

    row = {
        "asset": "BTC", "product_type": "15m", "strategy": "TAKER_NOW",
        "price_band_5c": 18, "stc_band_60s": 3, "cell_block_stage": "candidate",
        "n_30d": 60, "wr_30d": 0.95, "mean_cal_prob_30d": 0.89,
        "cal_gap_30d": -0.06, "persistence_days": 3, "cohort_date": "2026-05-12",
    }
    payload = format_calibration_payload(row)
    assert "-6.0pp" in payload  # not '+-6.0pp'
    assert "+-" not in payload


def test_cohort_dedup_key_distinguishes_bleed_and_cal_per_cohort():
    """`cohort_dedup_key` builds a stable per-cohort × per-kind key. BLEED
    and CAL on the SAME cohort must NOT collide in the 60s notifier dedup
    (different kinds = different alerts)."""
    from bot.helpers.cohort_alerts import cohort_dedup_key

    row = {
        "asset": "SOL", "product_type": "15m", "strategy": "MAKER_PATIENT",
        "price_band_5c": 17, "stc_band_60s": 8, "cell_block_stage": "candidate",
    }
    bleed_key = cohort_dedup_key(row, "bleed")
    cal_key = cohort_dedup_key(row, "cal")
    assert bleed_key != cal_key
    assert "bleed" in bleed_key
    assert "cal" in cal_key
    # Stable: same row → same key
    assert cohort_dedup_key(row, "bleed") == bleed_key


def test_emit_alert_returns_false_when_telegram_singleton_unset():
    """`emit_alert` does NOT raise when `bot.notifier._TELEGRAM` is None
    (boot-ordering case before MainLoop.__init__ runs, or test harness)."""
    import bot.notifier as _telegram_state
    from bot.helpers.cohort_alerts import emit_alert

    prior = _telegram_state._TELEGRAM
    _telegram_state._TELEGRAM = None
    try:
        row = {
            "asset": "SOL", "product_type": "15m", "strategy": "MAKER_PATIENT",
            "price_band_5c": 17, "stc_band_60s": 8, "cell_block_stage": "candidate",
            "n_30d": 138, "wr_30d": 0.841, "wilson95_hi_30d": 0.892,
            "cf_pnl_30d_dollars": -285.64, "cohort_date": "2026-05-12",
        }
        assert emit_alert(row, kind="bleed") is False
    finally:
        _telegram_state._TELEGRAM = prior


def test_emit_alert_calls_notifier_send_when_singleton_present():
    """`emit_alert` hands off to `_TELEGRAM.send(payload, dedup_key=...)`
    when the singleton is set and `enabled`. Uses a stub notifier — does
    NOT hit the real Telegram API."""
    import bot.notifier as _telegram_state
    from bot.helpers.cohort_alerts import emit_alert

    captured = {}

    class _StubNotifier:
        enabled = True

        def send(self, message, silent=False, dedup_key=None):
            captured["message"] = message
            captured["dedup_key"] = dedup_key

    prior = _telegram_state._TELEGRAM
    _telegram_state._TELEGRAM = _StubNotifier()
    try:
        row = {
            "asset": "SOL", "product_type": "15m", "strategy": "MAKER_PATIENT",
            "price_band_5c": 17, "stc_band_60s": 8, "cell_block_stage": "candidate",
            "n_30d": 138, "wr_30d": 0.841, "wilson95_hi_30d": 0.892,
            "cf_pnl_30d_dollars": -285.64, "cohort_date": "2026-05-12",
        }
        assert emit_alert(row, kind="bleed") is True
        assert "BLEED" in captured["message"]
        assert "SOL" in captured["message"]
        assert captured["dedup_key"] is not None
        assert "bleed" in captured["dedup_key"]
    finally:
        _telegram_state._TELEGRAM = prior


def test_persistence_days_breaks_on_date_gap():
    """Bundled followup 1 (R3 MN1 from kb resume doc): if the nightly
    cron misfires and leaves a gap in cohort_date series, the
    persistence counter must NOT silently treat the breach-before-gap
    and breach-after-gap as "consecutive" — it must reset on gap.

    Example: today=2026-05-12, prior rows {05-11, 05-09, 05-08} all
    breaching. The gap between 05-11 and 05-09 (missing 05-10) breaks
    persistence. Expected count: 1 (only 05-11 contiguous to today).
    """
    from bot.helpers.cohort_attribution import _compute_persistence_days, ensure_schema

    conn = sqlite3.connect(":memory:")
    ensure_schema(conn)
    cohort_key = ("SOL", "15m", "MAKER_PATIENT", 17, 8, "candidate")
    for date, gap in (("2026-05-11", 0.08), ("2026-05-09", 0.09), ("2026-05-08", 0.10)):
        conn.execute(
            "INSERT INTO cohort_attribution_daily (cohort_date, asset, product_type, "
            "strategy, price_band_5c, stc_band_60s, cell_block_stage, "
            "n_30d, n_yes_30d, n_no_30d, sum_cf_cents_30d, n_7d, "
            "cal_gap_30d) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (date, *cohort_key, 100, 80, 20, 0, 50, gap),
        )
    conn.commit()
    n = _compute_persistence_days(
        conn, cohort_date="2026-05-12", cohort_key=cohort_key, abs_threshold=0.05,
    )
    assert n == 1, f"expected 1 (gap-breaks-streak), got {n}"


def test_persistence_days_counts_contiguous_breaches():
    """Sanity: with no gaps, persistence counts every immediate-prior day
    that breached."""
    from bot.helpers.cohort_attribution import _compute_persistence_days, ensure_schema

    conn = sqlite3.connect(":memory:")
    ensure_schema(conn)
    cohort_key = ("SOL", "15m", "MAKER_PATIENT", 17, 8, "candidate")
    for date, gap in (("2026-05-11", 0.08), ("2026-05-10", 0.09), ("2026-05-09", 0.10)):
        conn.execute(
            "INSERT INTO cohort_attribution_daily (cohort_date, asset, product_type, "
            "strategy, price_band_5c, stc_band_60s, cell_block_stage, "
            "n_30d, n_yes_30d, n_no_30d, sum_cf_cents_30d, n_7d, "
            "cal_gap_30d) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (date, *cohort_key, 100, 80, 20, 0, 50, gap),
        )
    conn.commit()
    n = _compute_persistence_days(
        conn, cohort_date="2026-05-12", cohort_key=cohort_key, abs_threshold=0.05,
    )
    assert n == 3


class _FlakyConnProxy:
    """Proxy wrapping a real sqlite3.Connection that intercepts
    `executemany` to inject transient OperationalError('database is
    locked'). All other attribute accesses pass through to the real
    conn — necessary because `sqlite3.Connection.executemany` is a
    read-only C-level attribute and can't be monkey-patched directly."""

    def __init__(self, real_conn, fail_first_n_executemany: int):
        self._real = real_conn
        self._fail_n = fail_first_n_executemany
        self.executemany_call_count = 0

    def executemany(self, sql, rows):
        self.executemany_call_count += 1
        if self.executemany_call_count <= self._fail_n:
            raise sqlite3.OperationalError("database is locked")
        return self._real.executemany(sql, rows)

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_run_aggregation_retries_on_database_locked():
    """Bundled followup 2 (cf34b5c retry pattern, kb resume doc): wrap
    executemany in 3-retry-on-busy so the nightly cron silent-recovers
    from transient `database is locked` collisions with the bot's
    writer process.

    Proxy raises OperationalError('database is locked') the first 2
    times executemany is called and succeeds the third — must complete
    without propagating the exception.
    """
    from bot.helpers.cohort_attribution import run_aggregation

    real = sqlite3.connect(":memory:")
    _eval_opp_schema_min(real)
    eval_time = "2026-05-11T13:07:00+00:00"
    _insert_eval_row(
        real,
        ticker="KXSOL15M-X", asset="SOL", product_type="15m",
        strategy="MAKER_PATIENT", filter_stage="candidate",
        market_price=88, seconds_to_close=250.0,
        market_result="yes", counterfactual_pnl=1500,
        calibrated_prob=0.92, evaluation_time=eval_time,
    )
    real.commit()

    proxy = _FlakyConnProxy(real, fail_first_n_executemany=2)
    pinned_now = _dt.datetime(2026, 5, 12, 13, 7, tzinfo=_dt.timezone.utc)
    run_aggregation(proxy, alerts_module=None, cohort_date="2026-05-12", now=pinned_now)

    assert proxy.executemany_call_count >= 3, (
        f"expected ≥3 attempts (2 failures + 1 success), got {proxy.executemany_call_count}"
    )


def test_run_aggregation_emits_alert_on_quiet_to_firing_transition():
    """End-to-end wire: when a previously-quiet cohort first crosses the
    BLEED threshold today, run_aggregation calls alerts_module.emit_alert
    exactly once with kind='bleed' and the cohort identity.

    A brand-new cohort (prior_row is None) returns ('quiet', None) per
    R7 — so seed a prior 'quiet' row to put the transition test in the
    quiet→firing branch. A cohort that stays firing across multiple
    ticks under cooldown emits once-per-23h, not once-per-tick.
    """
    from bot.helpers.cohort_attribution import ensure_schema, run_aggregation

    conn = sqlite3.connect(":memory:")
    _eval_opp_schema_min(conn)
    ensure_schema(conn)
    # Seed prior aggregation row in 'quiet' state for same cohort.
    conn.execute(
        "INSERT INTO cohort_attribution_daily (cohort_date, asset, product_type, "
        "strategy, price_band_5c, stc_band_60s, cell_block_stage, "
        "n_30d, n_yes_30d, n_no_30d, sum_cf_cents_30d, n_7d, alert_state) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("2026-05-11", "SOL", "15m", "MAKER_PATIENT", 17, 4, "candidate",
         50, 40, 10, 0, 50, "quiet"),
    )
    eval_time = "2026-05-11T13:07:00+00:00"
    _insert_eval_row(
        conn,
        ticker="KX1", asset="SOL", product_type="15m",
        strategy="MAKER_PATIENT", filter_stage="candidate",
        market_price=88, seconds_to_close=250.0,
        market_result="yes", counterfactual_pnl=1500,
        calibrated_prob=0.92, evaluation_time=eval_time,
    )
    conn.commit()

    emitted = []

    class _ModWithEmit(_FakeAlertsModule):
        def emit_alert(self, row, *, kind):
            emitted.append((kind, row.get("asset"), row.get("strategy")))
            return True

    mod = _ModWithEmit(fire_bleed=True, fire_cal=False, cooldown=False)
    pinned_now = _dt.datetime(2026, 5, 12, 13, 7, tzinfo=_dt.timezone.utc)
    run_aggregation(conn, alerts_module=mod, cohort_date="2026-05-12", now=pinned_now)
    assert len(emitted) == 1, f"expected 1 emit on quiet→firing transition, got {len(emitted)}"
    kind, asset, strategy = emitted[0]
    assert kind == "bleed"
    assert asset == "SOL"
    assert strategy == "MAKER_PATIENT"


def test_run_aggregation_does_not_emit_cross_family_under_cooldown():
    """R1 M1 pin: a cohort that was `firing_bleed` yesterday and would
    fire `firing_cal` today MUST NOT emit a fresh CAL alert when the
    prior BLEED cooldown is still active.

    Design § Alert design says "23h cooldown per cohort" (not per-kind).
    The emit-gate uses `next_last_alert != prior_last_alert` to detect
    a fresh fire — when cooldown is active, `compute_next_alert_state`
    preserves the prior timestamp, and the emit-gate suppresses the
    cross-family Telegram.
    """
    from bot.helpers.cohort_attribution import ensure_schema, run_aggregation

    conn = sqlite3.connect(":memory:")
    _eval_opp_schema_min(conn)
    ensure_schema(conn)
    conn.execute(
        "INSERT INTO cohort_attribution_daily (cohort_date, asset, product_type, "
        "strategy, price_band_5c, stc_band_60s, cell_block_stage, "
        "n_30d, n_yes_30d, n_no_30d, sum_cf_cents_30d, n_7d, "
        "alert_state, last_alert_time) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("2026-05-11", "SOL", "15m", "MAKER_PATIENT", 17, 4, "candidate",
         100, 80, 20, -10000, 50, "firing_bleed", "2026-05-12T01:00:00+00:00"),
    )
    eval_time = "2026-05-11T13:07:00+00:00"
    _insert_eval_row(
        conn,
        ticker="KX1", asset="SOL", product_type="15m",
        strategy="MAKER_PATIENT", filter_stage="candidate",
        market_price=88, seconds_to_close=250.0,
        market_result="yes", counterfactual_pnl=1500,
        calibrated_prob=0.92, evaluation_time=eval_time,
    )
    conn.commit()

    emitted = []

    class _ModWithEmit(_FakeAlertsModule):
        def emit_alert(self, row, *, kind):
            emitted.append((kind, row.get("asset")))
            return True

    # BLEED no longer firing today, but CAL fires AND cooldown is still
    # active (~12h since prior fire at 01:00 UTC, today 13:07 UTC).
    mod = _ModWithEmit(fire_bleed=False, fire_cal=True, cooldown=True)
    pinned_now = _dt.datetime(2026, 5, 12, 13, 7, tzinfo=_dt.timezone.utc)
    run_aggregation(conn, alerts_module=mod, cohort_date="2026-05-12", now=pinned_now)
    assert emitted == [], (
        f"expected 0 emits (cross-family under cooldown), got {emitted}"
    )


def test_run_aggregation_emits_after_cooldown_expires():
    """Sanity: when a previously-firing cohort's cooldown has expired
    AND the cohort still meets firing criteria, emit fires fresh
    (because `compute_next_alert_state` stamps a new `now_iso` and the
    emit-gate sees `next_last_alert != prior_last_alert`)."""
    from bot.helpers.cohort_attribution import ensure_schema, run_aggregation

    conn = sqlite3.connect(":memory:")
    _eval_opp_schema_min(conn)
    ensure_schema(conn)
    # Prior fire 48h ago — cooldown expired.
    conn.execute(
        "INSERT INTO cohort_attribution_daily (cohort_date, asset, product_type, "
        "strategy, price_band_5c, stc_band_60s, cell_block_stage, "
        "n_30d, n_yes_30d, n_no_30d, sum_cf_cents_30d, n_7d, "
        "alert_state, last_alert_time) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("2026-05-10", "SOL", "15m", "MAKER_PATIENT", 17, 4, "candidate",
         100, 80, 20, -10000, 50, "firing_bleed", "2026-05-10T13:07:00+00:00"),
    )
    eval_time = "2026-05-11T13:07:00+00:00"
    _insert_eval_row(
        conn,
        ticker="KX1", asset="SOL", product_type="15m",
        strategy="MAKER_PATIENT", filter_stage="candidate",
        market_price=88, seconds_to_close=250.0,
        market_result="yes", counterfactual_pnl=1500,
        calibrated_prob=0.92, evaluation_time=eval_time,
    )
    conn.commit()

    emitted = []

    class _ModWithEmit(_FakeAlertsModule):
        def emit_alert(self, row, *, kind):
            emitted.append((kind, row.get("asset")))
            return True

    # Cohort still meets BLEED criteria; cooldown=False simulates the
    # ≥23h gap evaluation.
    mod = _ModWithEmit(fire_bleed=True, fire_cal=False, cooldown=False)
    pinned_now = _dt.datetime(2026, 5, 12, 13, 7, tzinfo=_dt.timezone.utc)
    run_aggregation(conn, alerts_module=mod, cohort_date="2026-05-12", now=pinned_now)
    assert len(emitted) == 1
    assert emitted[0] == ("bleed", "SOL")


def test_run_aggregation_emits_alert_on_quiet_to_firing_cal_transition():
    """R2 N2: pin the cal-side end-to-end emit path explicitly. The
    `kind = "bleed" if next_state == "firing_bleed" else "cal"` else-arm
    is unit-tested at the helper layer, but symmetry with the bleed pin
    closes the integration-level surface."""
    from bot.helpers.cohort_attribution import ensure_schema, run_aggregation

    conn = sqlite3.connect(":memory:")
    _eval_opp_schema_min(conn)
    ensure_schema(conn)
    # Seed prior 'quiet' so the transition is quiet → firing_cal.
    conn.execute(
        "INSERT INTO cohort_attribution_daily (cohort_date, asset, product_type, "
        "strategy, price_band_5c, stc_band_60s, cell_block_stage, "
        "n_30d, n_yes_30d, n_no_30d, sum_cf_cents_30d, n_7d, alert_state) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("2026-05-11", "ETH", "15m", "MAKER_PATIENT", 15, 9, "candidate",
         50, 40, 10, 0, 50, "quiet"),
    )
    eval_time = "2026-05-11T13:07:00+00:00"
    _insert_eval_row(
        conn,
        ticker="KX1", asset="ETH", product_type="15m",
        strategy="MAKER_PATIENT", filter_stage="candidate",
        market_price=78, seconds_to_close=560.0,
        market_result="yes", counterfactual_pnl=1000,
        calibrated_prob=0.91, evaluation_time=eval_time,
    )
    conn.commit()

    emitted = []

    class _ModWithEmit(_FakeAlertsModule):
        def emit_alert(self, row, *, kind):
            emitted.append((kind, row.get("asset")))
            return True

    mod = _ModWithEmit(fire_bleed=False, fire_cal=True, cooldown=False)
    pinned_now = _dt.datetime(2026, 5, 12, 13, 7, tzinfo=_dt.timezone.utc)
    run_aggregation(conn, alerts_module=mod, cohort_date="2026-05-12", now=pinned_now)
    assert len(emitted) == 1
    assert emitted[0] == ("cal", "ETH")


def test_run_aggregation_emits_cross_family_after_cooldown_expires():
    """R2 N3 + M1 symmetric pin: firing_bleed prior + CAL fires today +
    cooldown EXPIRED (prior fire 48h ago) → emits fresh CAL alert.
    Mirrors the same-family-cooldown-expired pin but for cross-family,
    closing the symmetry of the M1 fix's contract surface."""
    from bot.helpers.cohort_attribution import ensure_schema, run_aggregation

    conn = sqlite3.connect(":memory:")
    _eval_opp_schema_min(conn)
    ensure_schema(conn)
    conn.execute(
        "INSERT INTO cohort_attribution_daily (cohort_date, asset, product_type, "
        "strategy, price_band_5c, stc_band_60s, cell_block_stage, "
        "n_30d, n_yes_30d, n_no_30d, sum_cf_cents_30d, n_7d, "
        "alert_state, last_alert_time) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("2026-05-10", "SOL", "15m", "MAKER_PATIENT", 17, 4, "candidate",
         100, 80, 20, -10000, 50, "firing_bleed", "2026-05-10T13:07:00+00:00"),
    )
    eval_time = "2026-05-11T13:07:00+00:00"
    _insert_eval_row(
        conn,
        ticker="KX1", asset="SOL", product_type="15m",
        strategy="MAKER_PATIENT", filter_stage="candidate",
        market_price=88, seconds_to_close=250.0,
        market_result="yes", counterfactual_pnl=1500,
        calibrated_prob=0.92, evaluation_time=eval_time,
    )
    conn.commit()

    emitted = []

    class _ModWithEmit(_FakeAlertsModule):
        def emit_alert(self, row, *, kind):
            emitted.append((kind, row.get("asset")))
            return True

    # CAL fires today (not BLEED); cooldown=False (48h gap exceeds 23h).
    mod = _ModWithEmit(fire_bleed=False, fire_cal=True, cooldown=False)
    pinned_now = _dt.datetime(2026, 5, 12, 13, 7, tzinfo=_dt.timezone.utc)
    run_aggregation(conn, alerts_module=mod, cohort_date="2026-05-12", now=pinned_now)
    assert len(emitted) == 1
    assert emitted[0] == ("cal", "SOL")


def test_compute_next_alert_state_brand_new_cohort_with_firing_metrics_stays_quiet():
    """R2 N4: pin the day-1 latency contract — a brand-new cohort
    (prior_row=None) that already meets BLEED criteria still returns
    ('quiet', None). The alert first fires on day 2 when prior_row
    exists. This is intentional per R7 advisory to avoid alerting on
    a single-day spike without any prior history to compare against."""
    from bot.helpers.cohort_attribution import compute_next_alert_state

    class _FireEverything:
        def should_fire_bleed_alert(self, **_kw):
            return True

        def should_fire_calibration_alert(self, **_kw):
            return True

        def cooldown_active(self, **_kw):
            return False

    current_metrics = {
        "n_30d": 138,
        "wilson95_hi_30d": 0.892,
        "cf_pnl_30d_dollars": -285.64,
        "abs_cal_gap_30d": 0.08,
        "persistence_days": 3,
        "cell_block_stage": "candidate",
    }
    next_state, next_last_alert = compute_next_alert_state(
        None, current_metrics, alerts_module=_FireEverything(),
    )
    assert next_state == "quiet"
    assert next_last_alert is None


def test_emit_alert_unknown_kind_returns_false_and_logs():
    """MN5: emit_alert with an unknown `kind` value must log a warning
    (operator visibility) and return False (no Telegram dispatch)."""
    import bot.notifier as _telegram_state
    from bot.helpers.cohort_alerts import emit_alert

    class _StubNotifier:
        enabled = True
        last = None

        def send(self, message, silent=False, dedup_key=None):
            self.last = message

    prior = _telegram_state._TELEGRAM
    stub = _StubNotifier()
    _telegram_state._TELEGRAM = stub
    try:
        row = {
            "asset": "SOL", "product_type": "15m", "strategy": "MAKER_PATIENT",
            "price_band_5c": 17, "stc_band_60s": 8, "cell_block_stage": "candidate",
        }
        assert emit_alert(row, kind="banana") is False
        assert stub.last is None  # notifier never called
    finally:
        _telegram_state._TELEGRAM = prior


def test_run_aggregation_does_not_emit_when_state_unchanged():
    """If the cohort was already firing_bleed yesterday (prior row exists
    with that state) and stays firing today, no NEW emit fires —
    operator already alerted; the 23h cooldown carry covers this."""
    from bot.helpers.cohort_attribution import run_aggregation

    conn = sqlite3.connect(":memory:")
    _eval_opp_schema_min(conn)
    # Seed a prior aggregation row in firing_bleed state for the same cohort.
    from bot.helpers.cohort_attribution import ensure_schema
    ensure_schema(conn)
    conn.execute(
        "INSERT INTO cohort_attribution_daily (cohort_date, asset, product_type, "
        "strategy, price_band_5c, stc_band_60s, cell_block_stage, "
        "n_30d, n_yes_30d, n_no_30d, sum_cf_cents_30d, n_7d, "
        "alert_state, last_alert_time) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("2026-05-11", "SOL", "15m", "MAKER_PATIENT", 17, 4, "candidate",
         100, 80, 20, -10000, 50, "firing_bleed", "2026-05-11T13:07:00+00:00"),
    )
    eval_time = "2026-05-11T13:07:00+00:00"
    _insert_eval_row(
        conn,
        ticker="KX1", asset="SOL", product_type="15m",
        strategy="MAKER_PATIENT", filter_stage="candidate",
        market_price=88, seconds_to_close=250.0,
        market_result="yes", counterfactual_pnl=1500,
        calibrated_prob=0.92, evaluation_time=eval_time,
    )
    conn.commit()

    emitted = []

    class _ModWithEmit(_FakeAlertsModule):
        def emit_alert(self, row, *, kind):
            emitted.append((kind, row.get("asset")))
            return True

    mod = _ModWithEmit(fire_bleed=True, fire_cal=False, cooldown=True)
    pinned_now = _dt.datetime(2026, 5, 12, 13, 7, tzinfo=_dt.timezone.utc)
    run_aggregation(conn, alerts_module=mod, cohort_date="2026-05-12", now=pinned_now)
    # Same state across ticks; cooldown active → 0 emits.
    assert emitted == []


def test_run_aggregation_gives_up_after_max_retries():
    """If `database is locked` persists past the retry budget, the
    exception MUST propagate — silent-swallowing the OperationalError
    would lose the nightly aggregation without alerting the operator."""
    from bot.helpers.cohort_attribution import run_aggregation

    real = sqlite3.connect(":memory:")
    _eval_opp_schema_min(real)
    eval_time = "2026-05-11T13:07:00+00:00"
    _insert_eval_row(
        real,
        ticker="KXSOL15M-X", asset="SOL", product_type="15m",
        strategy="MAKER_PATIENT", filter_stage="candidate",
        market_price=88, seconds_to_close=250.0,
        market_result="yes", counterfactual_pnl=1500,
        calibrated_prob=0.92, evaluation_time=eval_time,
    )
    real.commit()

    proxy = _FlakyConnProxy(real, fail_first_n_executemany=999)
    pinned_now = _dt.datetime(2026, 5, 12, 13, 7, tzinfo=_dt.timezone.utc)
    with pytest.raises(sqlite3.OperationalError):
        run_aggregation(proxy, alerts_module=None, cohort_date="2026-05-12", now=pinned_now)
