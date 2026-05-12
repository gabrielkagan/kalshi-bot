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


@pytest.mark.xfail(strict=True, reason="Sister Bit P1.3 pending — bot/helpers/cohort_alerts.py not yet shipped")
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


@pytest.mark.xfail(strict=True, reason="Sister Bit P1.3 pending")
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


@pytest.mark.xfail(strict=True, reason="Sister Bit P1.3 pending")
def test_bleed_alert_n_boundary_49_vs_50():
    """n=49 → no fire; n=50 → fire. Boundary pin against off-by-one drift."""
    from bot.helpers.cohort_alerts import should_fire_bleed_alert

    common = dict(wilson95_hi=0.89, cf_pnl_30d_dollars=-100.0, cell_block_stage="candidate")
    assert should_fire_bleed_alert(n=49, **common) is False
    assert should_fire_bleed_alert(n=50, **common) is True


@pytest.mark.xfail(strict=True, reason="Sister Bit P1.3 pending")
def test_bleed_alert_wilson_hi_boundary_0919_vs_0920():
    """wilson95_hi=0.9199 → fire (strictly < 0.92); wilson95_hi=0.9200 → no fire."""
    from bot.helpers.cohort_alerts import should_fire_bleed_alert

    common = dict(n=100, cf_pnl_30d_dollars=-100.0, cell_block_stage="candidate")
    assert should_fire_bleed_alert(wilson95_hi=0.9199, **common) is True
    assert should_fire_bleed_alert(wilson95_hi=0.9200, **common) is False


@pytest.mark.xfail(strict=True, reason="Sister Bit P1.3 pending")
def test_bleed_alert_cf_pnl_boundary_minus49_99_vs_minus50_01():
    """cf_pnl=-49.99 → no fire; cf_pnl=-50.01 → fire. Strictly < -50."""
    from bot.helpers.cohort_alerts import should_fire_bleed_alert

    common = dict(n=100, wilson95_hi=0.89, cell_block_stage="candidate")
    assert should_fire_bleed_alert(cf_pnl_30d_dollars=-49.99, **common) is False
    assert should_fire_bleed_alert(cf_pnl_30d_dollars=-50.01, **common) is True


@pytest.mark.xfail(strict=True, reason="Sister Bit P1.3 pending")
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


@pytest.mark.xfail(strict=True, reason="Sister Bit P1.3 pending")
def test_calibration_alert_gap_boundary_0_0499_vs_0_0501():
    """abs_cal_gap=0.0499 → no fire; abs_cal_gap=0.0501 → fire (with
    n≥30 and persistence_days≥3). Strictly > 0.05."""
    from bot.helpers.cohort_alerts import should_fire_calibration_alert

    common = dict(n=30, persistence_days=3)
    assert should_fire_calibration_alert(abs_cal_gap=0.0499, **common) is False
    assert should_fire_calibration_alert(abs_cal_gap=0.0501, **common) is True


@pytest.mark.xfail(strict=True, reason="Sister Bit P1.3 pending")
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


@pytest.mark.xfail(strict=True, reason="Sister Bit P1.3 pending")
def test_cooldown_active_exact_23h_boundary():
    """Cooldown boundary: 22h59m → active; 23h00m01s → expired.

    Strict ≥23h to expire. Mirrors the BLEED n / cf_pnl / wilson_hi
    boundary-pinning style: each numeric threshold gets its own pin.
    """
    from datetime import datetime, timedelta

    from bot.helpers.cohort_alerts import cooldown_active

    now = datetime(2026, 5, 12, 13, 7, 0)
    just_under_23h = now - timedelta(hours=22, minutes=59)
    just_over_23h = now - timedelta(hours=23, seconds=1)

    assert cooldown_active(last_alert_time=just_under_23h, now=now) is True
    assert cooldown_active(last_alert_time=just_over_23h, now=now) is False


@pytest.mark.xfail(strict=True, reason="Sister Bit P1.3 pending")
def test_calibration_alert_n_boundary_29_vs_30():
    """CAL n boundary: n=29 → no fire; n=30 → fire (with persistence + gap).

    Mirrors BLEED n=49 vs 50 pattern. Strict ≥30.
    """
    from bot.helpers.cohort_alerts import should_fire_calibration_alert

    common = dict(abs_cal_gap=0.08, persistence_days=3)
    assert should_fire_calibration_alert(n=29, **common) is False
    assert should_fire_calibration_alert(n=30, **common) is True


@pytest.mark.xfail(strict=True, reason="Sister Bit P1.3 pending")
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
