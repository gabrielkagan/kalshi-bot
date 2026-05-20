"""F0.1 — Stale-quote sniping falsification: failing-assertion scaffold.

TDD-first scaffold per `CLAUDE.md` extraction-bit discipline. Lands BEFORE
implementation; all tests RED at scaffold-ship; transition to GREEN as
`scripts/research/f0_1_stale_quote_falsification.py` is implemented.

Invariants pinned here:

1. Schema invariants — required columns exist in moc + evaluated_opportunities + settled_trades.
2. No-look-ahead — script reads only rows with observation_time ≤ σ-move time for the stale snapshot.
   (R1-M1 regression: mixed timestamp precision must not break the invariant via lexicographic compare.)
3. Regime conditioning — vol-high vs. vol-low buckets produce distinct ceilings.
4. Bootstrap CI shape — (low, point, high) with low ≤ point ≤ high.
5. Verdict mapping — synthetic ceiling < $5K/yr per asset → KILL; ≥ $5K/yr on any asset → SURVIVE.
6. Hit-probability clamp — over-1.0 input triggers ERROR.
7. Size clamp — observed size > MAX_TAKE clamps to MAX_TAKE.
8. Kill-rule wiring — 7-asset end-to-end pipeline returns KILL when all sub-threshold.
9. Program-level diagnostics (R1-M5) — n_assets_clearing_threshold + assets_clearing_threshold
   exposed so the umbrella ≥2-of-3 gate has data without re-running F0.1.

Parent plan: kb/decisions/ct-mdp-f0-1-stale-quote-falsification-plan.md
Parent ClickUp: 86ba18zg8
"""

from __future__ import annotations

from pathlib import Path

import pytest

# Plain import — NOT importorskip — so that any future import-time error in
# the script (SyntaxError, missing dep, etc.) FAILS rather than silently
# greens the module per R1-M4. The script already exists; the skip was
# defensive against a no-longer-existing condition.
from scripts.research import f0_1_stale_quote_falsification as falsification


# ----- Schema invariants (Invariant 1) -----------------------------------


def test_moc_has_required_columns_for_f0_1():
    """moc must expose ticker, observation_time, yes_bid/ask, bid/ask_depth, cache_age_ms."""
    required = {
        "ticker", "observation_time",
        "yes_bid_cents", "yes_ask_cents",
        "no_bid_cents", "no_ask_cents",
        "bid_depth", "ask_depth",
        "cache_age_ms",
    }
    cols = falsification.MOC_REQUIRED_COLUMNS
    assert required.issubset(set(cols)), f"missing moc columns: {required - set(cols)}"


def test_evaluated_opportunities_has_spot_columns():
    """evaluated_opportunities must expose per-asset spot_at_decision columns."""
    required_spot = {
        "btc_spot_at_decision", "eth_spot_at_decision", "sol_spot_at_decision",
        "xrp_spot_at_decision", "hype_spot_at_decision", "doge_spot_at_decision",
        "bnb_spot_at_decision",
    }
    cols = falsification.EVAL_OPPS_SPOT_COLUMNS
    assert required_spot.issubset(set(cols)), \
        f"missing eval_opps spot columns: {required_spot - set(cols)}"


def test_settled_trades_has_asset_and_time():
    """settled_trades anchors the 7-asset universe + window time mapping."""
    cols = falsification.SETTLED_TRADES_REQUIRED_COLUMNS
    assert "asset" in cols
    assert "settled_at" in cols


# ----- No-look-ahead (Invariant 2) ---------------------------------------


def test_stale_snapshot_uses_only_pre_event_rows():
    """For a σ-move at t=T, the stale-quote snapshot must use only moc rows with observation_time ≤ T."""
    # Synthetic: 3 moc rows at t-2, t-1, t+1; σ-move at t.
    stale_rows = falsification.select_stale_snapshot(
        moc_rows=[
            {"observation_time": "2026-05-20T10:00:00Z", "mid_cents": 50},
            {"observation_time": "2026-05-20T10:00:30Z", "mid_cents": 51},
            {"observation_time": "2026-05-20T10:01:30Z", "mid_cents": 60},  # post-event
        ],
        sigma_move_time="2026-05-20T10:01:00Z",
    )
    # No row from after the σ-move time may appear in the stale set.
    event_dt = falsification._parse_iso("2026-05-20T10:01:00Z")
    for row in stale_rows:
        assert falsification._parse_iso(row["observation_time"]) <= event_dt, \
            f"look-ahead violation: {row['observation_time']}"


def test_stale_snapshot_handles_mixed_timestamp_precision():
    """Regression for R1-M1: lexicographic string compare would have admitted a microsecond-precision row in the same second AFTER a second-precision event."""
    # `"2026-05-20T10:01:00.999999Z"` is ~999ms AFTER `"2026-05-20T10:01:00Z"`.
    # Lexicographic compare: `.` (0x2E) < `Z` (0x5A) so the post-event row
    # would have been wrongly included. Numeric datetime compare excludes it.
    stale_rows = falsification.select_stale_snapshot(
        moc_rows=[
            {"observation_time": "2026-05-20T10:00:59.500000Z"},   # pre-event, included
            {"observation_time": "2026-05-20T10:01:00.999999Z"},   # post-event, MUST be excluded
        ],
        sigma_move_time="2026-05-20T10:01:00Z",
    )
    obs_times = [r["observation_time"] for r in stale_rows]
    assert "2026-05-20T10:00:59.500000Z" in obs_times, "pre-event row dropped"
    assert "2026-05-20T10:01:00.999999Z" not in obs_times, \
        "look-ahead violation: post-event microsecond row included via lexicographic compare"


# ----- Regime conditioning (Invariant 3) ----------------------------------


def test_vol_high_and_low_buckets_are_separate():
    """Per-asset ceiling MUST be reported per vol-regime bucket, not aggregated."""
    result = falsification.compute_ceiling(
        events=[],  # empty synthetic event set
        asset="BTC",
        regime_conditioned=True,
    )
    assert "vol_high" in result
    assert "vol_low" in result
    assert "day" in result
    assert "night" in result


# ----- Bootstrap CI shape (Invariant 4) ----------------------------------


def test_bootstrap_ci_orders_low_point_high():
    """Bootstrap CI must satisfy low ≤ point ≤ high."""
    estimate = falsification.bootstrap_ceiling_ci(
        per_event_values=[100.0, 200.0, 150.0, 175.0, 125.0] * 20,  # 100 events
        n_resamples=1000,
        seed=42,
    )
    assert estimate["ci_low"] <= estimate["point"] <= estimate["ci_high"]


# ----- Verdict mapping (Invariant 5) -------------------------------------


def test_verdict_killed_below_5k_per_year():
    """Synthetic per-asset ceilings all < $5K annualized → KILL."""
    verdict = falsification.classify_verdict(
        per_asset_ceilings={
            "BTC": 1000.0, "ETH": 2000.0, "SOL": 500.0,
            "XRP": 3000.0, "HYPE": 1500.0, "DOGE": 1000.0, "BNB": 800.0,
        },
        threshold_dollars=5000.0,
    )
    assert verdict == "KILL"


def test_verdict_survive_when_one_asset_clears_threshold():
    """At least one asset ≥ $5K annualized → SURVIVE."""
    verdict = falsification.classify_verdict(
        per_asset_ceilings={
            "BTC": 1000.0, "ETH": 2000.0, "SOL": 500.0,
            "XRP": 3000.0, "HYPE": 1500.0, "DOGE": 1000.0, "BNB": 8000.0,  # survivor
        },
        threshold_dollars=5000.0,
    )
    assert verdict == "SURVIVE"


# ----- Anti-fantasy clamps (Invariants 6 + 7) ----------------------------


def test_hit_probability_over_one_raises():
    """Computed hit_probability > 1.0 indicates a methodology bug — must raise."""
    with pytest.raises(ValueError, match="hit_probability"):
        falsification.aggregate_event_value(
            dislocation_cents=5.0,
            available_size=10,
            hit_probability=1.5,  # nonsensical
        )


def test_size_clamps_to_max_take():
    """Observed available_size larger than MAX_TAKE clamps to MAX_TAKE (anti-fantasy)."""
    value = falsification.aggregate_event_value(
        dislocation_cents=5.0,
        available_size=500,  # whale size
        hit_probability=0.5,
    )
    # MAX_TAKE = 100; clamped value should equal 5 cents × 100 × 0.5 = 250 cents = $2.50
    expected_max_take_value = 5.0 * 100 * 0.5
    assert value <= expected_max_take_value + 1e-6, \
        f"size did not clamp to MAX_TAKE: value={value}, expected ≤ {expected_max_take_value}"


# ----- Program-level diagnostics (R1-M5) ---------------------------------


def test_survival_diagnostics_exposes_per_asset_clear_count():
    """The umbrella program-level gate (≥2 of 3 falsifications survive) consumes per-asset detail. F0.1 must expose n_assets_clearing_threshold so the umbrella gate has data without re-running F0.1."""
    diag = falsification.survival_diagnostics(
        per_asset_ceilings={
            "BTC": 1000.0, "ETH": 2000.0, "SOL": 500.0,
            "XRP": 3000.0, "HYPE": 6000.0, "DOGE": 1000.0, "BNB": 8000.0,
        },
        threshold_dollars=5000.0,
    )
    assert diag["verdict"] == "SURVIVE"
    assert diag["n_assets_clearing_threshold"] == 2
    assert diag["assets_clearing_threshold"] == ["BNB", "HYPE"]
    assert diag["threshold_dollars"] == 5000.0


def test_survival_diagnostics_kill_zero_clearing():
    """KILL verdict + n_assets_clearing_threshold=0 when all assets sub-threshold."""
    diag = falsification.survival_diagnostics(
        per_asset_ceilings={"BTC": 100.0, "ETH": 200.0},
        threshold_dollars=5000.0,
    )
    assert diag["verdict"] == "KILL"
    assert diag["n_assets_clearing_threshold"] == 0
    assert diag["assets_clearing_threshold"] == []


# ----- Kill-rule wiring (Invariant 8) ------------------------------------


def test_full_pipeline_kill_when_all_assets_subthreshold(tmp_path: Path):
    """End-to-end: 7-asset synthetic run where all ceilings are < $5K → KILL verdict in output dict."""
    db_path = tmp_path / "synthetic_state.db"
    _seed_synthetic_subthreshold_db(db_path)
    result = falsification.main(
        db_path=str(db_path),
        days=5,
        sigma_threshold=0.3,
        bootstrap_n=100,  # small for test speed
    )
    assert result["verdict"] == "KILL"
    assert all(c < 5000.0 for c in result["per_asset_ceilings"].values())


def _seed_synthetic_subthreshold_db(db_path: Path) -> None:
    """Create a minimal state.db with 7 assets, all sub-threshold dislocations.

    Helper for the kill-rule wiring test. Leave empty / skip until the
    script's data-loading layer is implemented and the test fixture format
    is defined by R1.
    """
    pytest.skip("synthetic-db fixture not yet defined (lands with implementation)")
