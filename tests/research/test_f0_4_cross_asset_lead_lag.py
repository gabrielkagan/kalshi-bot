"""F0.4 — Cross-Asset Lead-Lag Falsification test suite.

TDD-first scaffold per `CLAUDE.md` extraction-bit discipline + the
F0.1 precedent (`tests/research/test_f0_1_stale_quote_falsification.py`,
SHIPPED PR #132 verdict SURVIVE — see plan-doc § References for the
template lineage). Lands BEFORE implementation as **18 failing-assertion
test functions covering 13 logical invariants** with per-invariant
breakdown `1=4, 2=2, 3=1, 4=1, 5=1, 6=2, 7=1, 8=1, 9=1, 10=1, 11=1,
12=1, 13=1` (Invariant 1's 4 tests are the 4 schema-pin assertions;
Invariant 2 has paired M_stale / M_responded tests; Invariant 6 has
paired kill/survive verdict tests; Invariants 12+13 are paired
full-pipeline kill/survive). All tests RED at scaffold-ship (impl
helpers do not exist yet); flip GREEN as the impl-Bit lands per
TDD-first staging.

Adversarial-review prefix convention: `scaffold-R*` for the scaffold
gate, `impl-R*` for the impl gate (per F0.1 R14 lesson — keeps
finding-marker namespaces disambiguated across rounds).

Invariants pinned here:

1. Schema invariants — required columns exist in moc + evaluated_opportunities.
   6-laggard universe pinned in-script via LAGGARD_ASSETS; 7-asset universe
   pinned via ASSET_TICKER_PREFIX (BTC needed for σ-event detection).
2. No-look-ahead — script reads only rows with observation_time ≤ t_C for
   M_stale, > t_C for M_responded.
3. No-look-ahead mixed-precision regression — lexicographic string compare
   admits microsecond-precision row in the same second AFTER a
   second-precision event; numeric datetime compare must exclude it
   (F0.1 scaffold-R1-M1 precedent).
4. Regime conditioning — 4 buckets per laggard (vol_high_day,
   vol_high_night, vol_low_day, vol_low_night) produce distinct
   cell estimates.
5. Bootstrap CI shape — (low, point, high) with low ≤ point ≤ high;
   Bonferroni-adjusted CI wider than standard 95% for identical inputs.
6. Verdict mapping — synthetic per-cell lead-edge < 3¢ on all 24 cells →
   KILL; ≥1 cell with Bonferroni-adjusted CI lower-bound > 3¢ → SURVIVE.
7. Anti-fantasy clamp — lead-edge of 50¢ in 30s triggers ERROR
   (> MAX_PLAUSIBLE_EDGE_CENTS=25).
8. Sample-size insufficient — cell with < 30 events reports "insufficient".
9. Lead-direction sign — BTC down-move paired with laggard up-move
   produces a NEGATIVE lead-edge (signed by BTC direction).
10. Bonferroni correction wiring — bonferroni_n_tests=24 produces per-cell
    α = 0.05/24; n_tests=1 produces α = 0.05 (no correction).
11. Program-level diagnostics — survival_diagnostics() exposes
    n_cells_clearing_threshold + cells_clearing_threshold so the umbrella
    ≥2-of-3 gate has data without re-running F0.4.
12. Full pipeline kill — end-to-end synthetic run where all 24 cells are
    < 3¢ Bonferroni-adjusted → KILL verdict.
13. Full pipeline survive — end-to-end synthetic run where 1 cell ≥ 3¢
    Bonferroni-adjusted → SURVIVE verdict.

Parent plan: kb/decisions/ct-mdp-f0-4-cross-asset-lead-lag-plan.md
Parent ClickUp: 86ba18zgx
Predecessor (template): tests/research/test_f0_1_stale_quote_falsification.py
"""

from __future__ import annotations

from pathlib import Path

import pytest

# Plain import — NOT importorskip — so that any future import-time error
# in the script (SyntaxError, missing dep, etc.) FAILS rather than
# silently greens the module (per F0.1 scaffold-R1-M4 precedent). The
# script exists as a stub at scaffold-ship; the helpers below
# (referenced via `lead_lag.<name>`) DO NOT yet exist, so every test
# that touches an impl helper raises AttributeError at attribute-lookup
# time → RED on first run.
from scripts.research import f0_4_cross_asset_lead_lag as lead_lag


# ----- Schema invariants (Invariant 1) -----------------------------------


def test_moc_has_required_columns_for_f0_4():
    """moc must expose ticker, observation_time, yes_bid/ask (for laggard mid)."""
    required = {
        "ticker", "observation_time",
        "yes_bid_cents", "yes_ask_cents",
        "no_bid_cents", "no_ask_cents",
    }
    cols = lead_lag.MOC_REQUIRED_COLUMNS  # AttributeError at scaffold-ship → RED
    assert required.issubset(set(cols)), f"missing moc columns: {required - set(cols)}"


def test_evaluated_opportunities_has_spot_columns():
    """evaluated_opportunities must expose per-asset spot_at_decision columns."""
    required_spot = {
        "btc_spot_at_decision", "eth_spot_at_decision", "sol_spot_at_decision",
        "xrp_spot_at_decision", "hype_spot_at_decision", "doge_spot_at_decision",
        "bnb_spot_at_decision",
    }
    cols = lead_lag.EVAL_OPPS_SPOT_COLUMNS  # AttributeError at scaffold-ship → RED
    assert required_spot.issubset(set(cols)), \
        f"missing eval_opps spot columns: {required_spot - set(cols)}"


def test_six_laggard_universe_pinned_in_script():
    """6-laggard universe (BTC excluded — it's the leader) hardcoded via LAGGARD_ASSETS."""
    expected = {"ETH", "SOL", "XRP", "HYPE", "DOGE", "BNB"}
    assert set(lead_lag.LAGGARD_ASSETS) == expected  # AttributeError → RED


def test_seven_asset_ticker_prefix_includes_btc():
    """7-asset ticker prefix map includes BTC (used as the leader for σ-event detection)."""
    expected = {"BTC", "ETH", "SOL", "XRP", "HYPE", "DOGE", "BNB"}
    assert set(lead_lag.ASSET_TICKER_PREFIX.keys()) == expected  # AttributeError → RED


# ----- No-look-ahead (Invariants 2 + 3) ----------------------------------


def test_m_stale_uses_only_pre_event_rows():
    """For a BTC σ-event at t_C, M_stale must use only moc rows with observation_time ≤ t_C."""
    stale_row = lead_lag.select_m_stale(  # AttributeError → RED
        moc_rows=[
            {"observation_time": "2026-05-20T10:00:00Z", "mid_cents": 50},
            {"observation_time": "2026-05-20T10:00:30Z", "mid_cents": 51},
            {"observation_time": "2026-05-20T10:01:30Z", "mid_cents": 60},  # post-event
        ],
        sigma_event_time="2026-05-20T10:01:00Z",
    )
    event_dt = lead_lag._parse_iso("2026-05-20T10:01:00Z")
    assert lead_lag._parse_iso(stale_row["observation_time"]) <= event_dt, \
        f"look-ahead violation: M_stale used {stale_row['observation_time']}"


def test_m_responded_uses_only_post_event_rows():
    """For a BTC σ-event at t_C with Δt=30s, M_responded must use a moc row with t_C < observation_time ≤ t_C + Δt."""
    responded_row = lead_lag.select_m_responded(  # AttributeError → RED
        moc_rows=[
            {"observation_time": "2026-05-20T10:00:30Z", "mid_cents": 50},  # pre-event
            {"observation_time": "2026-05-20T10:01:20Z", "mid_cents": 55},  # in window
            {"observation_time": "2026-05-20T10:01:45Z", "mid_cents": 60},  # past window
        ],
        sigma_event_time="2026-05-20T10:01:00Z",
        delta_t_seconds=30.0,
    )
    event_dt = lead_lag._parse_iso("2026-05-20T10:01:00Z")
    obs_dt = lead_lag._parse_iso(responded_row["observation_time"])
    assert event_dt < obs_dt, f"look-ahead violation: M_responded ≤ t_C ({responded_row['observation_time']})"
    assert (obs_dt - event_dt).total_seconds() <= 30.0, \
        f"M_responded outside Δt window: {responded_row['observation_time']}"


def test_m_stale_handles_mixed_timestamp_precision():
    """Regression for F0.1 scaffold-R1-M1 precedent: lexicographic string compare admits a microsecond-precision row in the same second AFTER a second-precision event. Numeric datetime compare excludes it."""
    stale_row = lead_lag.select_m_stale(  # AttributeError → RED
        moc_rows=[
            {"observation_time": "2026-05-20T10:00:59.500000Z", "mid_cents": 50},  # pre-event
            {"observation_time": "2026-05-20T10:01:00.999999Z", "mid_cents": 99},  # post-event
        ],
        sigma_event_time="2026-05-20T10:01:00Z",
    )
    assert stale_row["observation_time"] == "2026-05-20T10:00:59.500000Z", \
        "look-ahead violation: post-event microsecond row included via lexicographic compare"


# ----- Regime conditioning (Invariant 4) ---------------------------------


def test_four_regimes_buckets_are_separate():
    """Per-laggard lead-edge MUST be reported per (vol-regime × day-night) bucket, not aggregated."""
    result = lead_lag.compute_lead_edge(  # AttributeError → RED
        events=[],  # empty synthetic event set
        laggard="ETH",
        regime_conditioned=True,
    )
    assert "vol_high_day" in result
    assert "vol_high_night" in result
    assert "vol_low_day" in result
    assert "vol_low_night" in result


# ----- Bootstrap CI shape + Bonferroni (Invariants 5 + 10) ---------------


def test_bootstrap_ci_orders_low_point_high():
    """Bootstrap CI must satisfy low ≤ point ≤ high."""
    estimate = lead_lag.bootstrap_lead_edge_ci(  # AttributeError → RED
        per_event_lead_edges=[1.0, 2.0, 1.5, 1.8, 1.3] * 20,  # 100 events
        n_resamples=1000,
        seed=42,
        confidence=0.95,
    )
    assert estimate["ci_low"] <= estimate["point"] <= estimate["ci_high"]


def test_bonferroni_adjusted_ci_is_wider_than_standard():
    """For identical inputs, Bonferroni-adjusted CI (n_tests=24) is wider than the standard 95% bootstrap CI (confidence=0.95, no Bonferroni)."""
    per_event = [1.0, 2.0, 1.5, 1.8, 1.3, 0.9, 2.2, 1.4, 1.7, 1.6] * 10  # 100 events
    standard = lead_lag.bootstrap_lead_edge_ci(  # AttributeError → RED
        per_event_lead_edges=per_event,
        n_resamples=1000,
        seed=42,
        confidence=0.95,
    )
    bonferroni = lead_lag.bonferroni_adjusted_ci(  # AttributeError → RED
        per_event_lead_edges=per_event,
        n_resamples=1000,
        seed=42,
        n_tests=24,
    )
    standard_width = standard["ci_high"] - standard["ci_low"]
    bonferroni_width = bonferroni["ci_high"] - bonferroni["ci_low"]
    assert bonferroni_width > standard_width, \
        f"Bonferroni CI not wider: standard={standard_width}, bonferroni={bonferroni_width}"


# ----- Verdict mapping (Invariant 6) -------------------------------------


def test_verdict_killed_when_all_cells_subthreshold():
    """All 24 cells with Bonferroni-adjusted CI lower-bound < 3¢ → KILL."""
    # Mock: 6 laggards × 4 regimes; every cell's ci_low_bonferroni < 3.0
    per_cell_results = {}
    for laggard in ("ETH", "SOL", "XRP", "HYPE", "DOGE", "BNB"):
        for regime in ("vol_high_day", "vol_high_night", "vol_low_day", "vol_low_night"):
            per_cell_results[(laggard, regime)] = {
                "point": 1.0, "ci_low_bonferroni": 0.5, "ci_high_bonferroni": 1.5,
                "n_events": 50,
            }
    verdict = lead_lag.classify_verdict(  # AttributeError → RED
        per_cell_results=per_cell_results,
        threshold_cents=3.0,
    )
    assert verdict == "KILL"


def test_verdict_survive_when_one_cell_clears_threshold():
    """≥1 cell with Bonferroni-adjusted CI lower-bound > 3¢ → SURVIVE."""
    per_cell_results = {}
    for laggard in ("ETH", "SOL", "XRP", "HYPE", "DOGE", "BNB"):
        for regime in ("vol_high_day", "vol_high_night", "vol_low_day", "vol_low_night"):
            per_cell_results[(laggard, regime)] = {
                "point": 1.0, "ci_low_bonferroni": 0.5, "ci_high_bonferroni": 1.5,
                "n_events": 50,
            }
    # One survivor cell
    per_cell_results[("ETH", "vol_high_day")] = {
        "point": 4.5, "ci_low_bonferroni": 3.5, "ci_high_bonferroni": 5.5,
        "n_events": 50,
    }
    verdict = lead_lag.classify_verdict(  # AttributeError → RED
        per_cell_results=per_cell_results,
        threshold_cents=3.0,
    )
    assert verdict == "SURVIVE"


# ----- Anti-fantasy clamp (Invariant 7) ----------------------------------


def test_lead_edge_over_max_plausible_raises():
    """Computed |lead_edge_cents| > MAX_PLAUSIBLE_EDGE_CENTS (25) → ERROR."""
    with pytest.raises(ValueError, match="MAX_PLAUSIBLE_EDGE_CENTS"):
        lead_lag.validate_lead_edge(  # AttributeError → RED
            lead_edge_cents=50.0,  # > 25 cents in 30s — implausible
        )


# ----- Sample-size insufficient (Invariant 8) -----------------------------


def test_cell_below_min_events_reports_insufficient():
    """Cell with < 30 events reports 'insufficient' instead of a noisy point estimate."""
    result = lead_lag.compute_lead_edge(  # AttributeError → RED
        events=[{"lead_edge_cents": 1.0, "regime": "vol_high_day"}] * 10,  # 10 < 30
        laggard="ETH",
        regime_conditioned=True,
    )
    assert result["vol_high_day"]["status"] == "insufficient"
    assert result["vol_high_day"]["n_events"] == 10


# ----- Lead-direction sign (Invariant 9) ----------------------------------


def test_lead_edge_signed_by_btc_direction():
    """BTC down-move paired with laggard up-move produces NEGATIVE lead-edge (laggard already moved opposite to BTC)."""
    event = lead_lag.compute_event_lead_edge(  # AttributeError → RED
        m_stale_cents=50,
        m_responded_cents=55,        # laggard moved UP
        btc_return_at_event=-0.005,  # BTC moved DOWN
    )
    assert event["lead_edge_cents"] < 0, \
        f"sign violation: BTC↓ + laggard↑ should be NEGATIVE, got {event['lead_edge_cents']}"


# ----- Program-level diagnostics (Invariant 11) --------------------------


def test_survival_diagnostics_exposes_per_cell_clear_count():
    """survival_diagnostics() must expose n_cells_clearing_threshold + cells_clearing_threshold so the umbrella ≥2-of-3 gate has data without re-running F0.4."""
    per_cell_results = {
        ("ETH", "vol_high_day"): {"ci_low_bonferroni": 3.5, "n_events": 50},
        ("ETH", "vol_low_day"): {"ci_low_bonferroni": 0.5, "n_events": 50},
        ("SOL", "vol_high_night"): {"ci_low_bonferroni": 4.2, "n_events": 50},
        ("BNB", "vol_low_night"): {"ci_low_bonferroni": -1.0, "n_events": 50},
    }
    diag = lead_lag.survival_diagnostics(  # AttributeError → RED
        per_cell_results=per_cell_results,
        threshold_cents=3.0,
    )
    assert diag["verdict"] == "SURVIVE"
    assert diag["n_cells_clearing_threshold"] == 2
    assert ("ETH", "vol_high_day") in diag["cells_clearing_threshold"]
    assert ("SOL", "vol_high_night") in diag["cells_clearing_threshold"]
    assert diag["threshold_cents"] == 3.0


# ----- Full pipeline (Invariants 12 + 13) --------------------------------


def test_full_pipeline_kill_when_all_cells_subthreshold(tmp_path: Path):
    """End-to-end: synthetic 7-asset run where all 24 cells are < 3¢ Bonferroni-adjusted → KILL."""
    db_path = tmp_path / "synthetic_state.db"
    _seed_synthetic_subthreshold_db(db_path)
    result = lead_lag.main(  # AttributeError → RED
        db_path=str(db_path),
        days=5,
        sigma_threshold=0.3,
        delta_t_seconds=30.0,
        bootstrap_n=100,  # small for test speed
        bonferroni_n_tests=24,
    )
    assert result["verdict"] == "KILL"
    assert all(
        cell["ci_low_bonferroni"] < 3.0
        for cell in result["per_cell_results"].values()
        if cell.get("status") != "insufficient"
    )


def test_full_pipeline_survive_when_one_cell_clears(tmp_path: Path):
    """End-to-end: synthetic run engineered so 1 cell clears 3¢ Bonferroni-adjusted → SURVIVE."""
    db_path = tmp_path / "synthetic_survive_state.db"
    _seed_synthetic_survivor_db(db_path)
    result = lead_lag.main(  # AttributeError → RED
        db_path=str(db_path),
        days=5,
        sigma_threshold=0.3,
        delta_t_seconds=30.0,
        bootstrap_n=100,
        bonferroni_n_tests=24,
    )
    assert result["verdict"] == "SURVIVE"
    assert result["n_cells_clearing_threshold"] >= 1


# ----- Synthetic-DB fixtures (impl-Bit fills bodies) ---------------------


def _seed_synthetic_subthreshold_db(db_path: Path) -> None:
    """Create a minimal state.db with all 24 cells sub-threshold (< 3¢ Bonferroni-adjusted).

    Impl-Bit fills the body per the F0.1 _seed_synthetic_subthreshold_db
    precedent (sqlite3 raw DDL mirroring only the columns the script
    reads). At scaffold-ship this stub raises NotImplementedError so the
    full-pipeline tests RED at this fixture call before reaching the
    impl helpers.
    """
    raise NotImplementedError("scaffold-only fixture; impl pending")


def _seed_synthetic_survivor_db(db_path: Path) -> None:
    """Create a minimal state.db engineered so 1 cell clears 3¢ Bonferroni-adjusted.

    Impl-Bit fills the body. At scaffold-ship raises NotImplementedError.
    """
    raise NotImplementedError("scaffold-only fixture; impl pending")
