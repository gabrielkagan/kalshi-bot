"""Markout-centric market-making evaluator — TDD-first.

The MM-native edge signal is MARKOUT: for a (shadow) maker fill, how does the
reliable mid move relative to our fill price over time? It decomposes into:

    markout(Δ) = spread_captured + adverse_selection(Δ)

  - spread_captured = side_mid_at_fill − fill_price   (we filled inside the spread)
  - adverse_selection(Δ) = side_mid(t+Δ) − side_mid_at_fill   (post-fill drift; the
    market moving AGAINST our resting side is negative = we got picked off)
  - markout(Δ) = side_mid(t+Δ) − fill_price            (total mark-to-mid PnL)

A market-maker is profitable iff spread_captured > −adverse_selection net of fees.
Negative markout = we get picked off (can't compete in that cell); positive = the
book is soft enough that we're the sharper quoter (we CAN compete there).

All prices in CENTS, in the FILLED SIDE's own units (YES fill → yes-mid; NO fill →
no-mid = 100 − yes-mid), so the primitives are side-symmetric. Settlement markout
marks to the binary outcome (side wins → 100 − fill_price; loses → −fill_price).

Parent: kb/decisions/settlement-convergence-worklist.md (markout MM evaluator)
"""
from __future__ import annotations

import pytest

from scripts.research import mm_markout_evaluator as mm


# ----- price-space primitives --------------------------------------------


def test_yes_mid_is_midpoint():
    assert mm.yes_mid(40, 50) == pytest.approx(45.0)


def test_side_mid_converts_no_side_to_100_minus_yes():
    assert mm.side_mid(45.0, "yes") == pytest.approx(45.0)
    assert mm.side_mid(45.0, "no") == pytest.approx(55.0)


# ----- markout decomposition (the core signal) ---------------------------


def test_markout_total_equals_side_mid_future_minus_fill():
    # YES fill at 40; yes-mid rises 45 -> 50 after we fill. We're long YES -> profit.
    assert mm.markout_cents(side_mid_future=50.0, fill_price=40.0) == pytest.approx(10.0)


def test_decompose_favorable_fill():
    # filled at 40, mid_at_fill 45 (captured 5 of spread), mid drifted UP to 50.
    d = mm.decompose_markout(fill_price=40.0, side_mid_at_fill=45.0, side_mid_future=50.0)
    assert d["spread_captured"] == pytest.approx(5.0)
    assert d["adverse_selection"] == pytest.approx(5.0)
    assert d["markout"] == pytest.approx(10.0)
    # invariant: spread + adverse == total markout
    assert d["spread_captured"] + d["adverse_selection"] == pytest.approx(d["markout"])


def test_decompose_toxic_fill_picked_off():
    # filled at 40, mid_at_fill 45, but mid COLLAPSED to 30 right after -> picked off.
    d = mm.decompose_markout(fill_price=40.0, side_mid_at_fill=45.0, side_mid_future=30.0)
    assert d["spread_captured"] == pytest.approx(5.0)
    assert d["adverse_selection"] == pytest.approx(-15.0)  # mid moved against us
    assert d["markout"] == pytest.approx(-10.0)             # spread eaten + more
    assert d["spread_captured"] + d["adverse_selection"] == pytest.approx(d["markout"])


def test_decompose_no_side_uses_no_mid_units():
    # NO fill at 30. yes_mid_at_fill=60 -> no_mid_at_fill=40 -> captured 10.
    # yes drifts UP to 70 -> no_mid_future=30 -> adverse for NO = 30-40 = -10.
    no_mid_at_fill = mm.side_mid(60.0, "no")   # 40
    no_mid_future = mm.side_mid(70.0, "no")    # 30
    d = mm.decompose_markout(fill_price=30.0, side_mid_at_fill=no_mid_at_fill,
                             side_mid_future=no_mid_future)
    assert d["spread_captured"] == pytest.approx(10.0)
    assert d["adverse_selection"] == pytest.approx(-10.0)
    assert d["markout"] == pytest.approx(0.0)


# ----- settlement markout -------------------------------------------------


def test_settlement_markout_yes():
    assert mm.settlement_markout_cents(40.0, "yes", "yes") == pytest.approx(60.0)
    assert mm.settlement_markout_cents(40.0, "yes", "no") == pytest.approx(-40.0)


def test_settlement_markout_no():
    assert mm.settlement_markout_cents(30.0, "no", "no") == pytest.approx(70.0)
    assert mm.settlement_markout_cents(30.0, "no", "yes") == pytest.approx(-30.0)


# ----- pre-registered KILL CRITERIA (WWJD: locked before we see data) -----


def test_gate_kills_when_settlement_markout_ci_includes_zero():
    # A cell with mean settlement markout >0 but a CI straddling zero is NOT an edge.
    # (small-n / noisy) -> KILL.
    verdict = mm.cell_gate(
        settlement_markouts=[60, -40, 60, -40, 60],  # mean +12 but huge variance
        markouts_30s=[5, -5, 5, -5, 5],
        fee_cents=1.0, n_fills=5, min_fills=30,
    )
    assert verdict["survives"] is False
    assert "n_fills" in verdict["fail_reasons"]  # n below floor is itself fatal


def test_gate_kills_when_negative_markout_at_horizon():
    # Even with positive settlement, if the 30s markout (adverse-selection proxy)
    # is net-negative, we're getting picked off -> KILL (not a capturable edge).
    verdict = mm.cell_gate(
        settlement_markouts=[60] * 50,
        markouts_30s=[-3] * 50,          # consistently picked off at 30s
        fee_cents=1.0, n_fills=50, min_fills=30,
    )
    assert verdict["survives"] is False
    assert "negative_markout_30s" in verdict["fail_reasons"]


def test_gate_survives_only_when_robustly_positive_net_of_fees():
    # Positive + low-variance at BOTH the 30s horizon AND settlement, n above floor,
    # net of fees -> the only configuration that SURVIVES.
    verdict = mm.cell_gate(
        settlement_markouts=[8] * 60,
        markouts_30s=[6] * 60,
        fee_cents=1.0, n_fills=60, min_fills=30,
    )
    assert verdict["survives"] is True
    assert verdict["fail_reasons"] == []


def test_bootstrap_ci_lower_bound_is_below_mean():
    lo, hi = mm.bootstrap_ci([5.0] * 100, n_boot=200)
    assert lo == pytest.approx(5.0, abs=1e-6) and hi == pytest.approx(5.0, abs=1e-6)
    lo2, hi2 = mm.bootstrap_ci([10, -10, 10, -10, 10, -10], n_boot=500)
    assert lo2 < 0 < hi2  # high-variance sample straddles zero


# ----- latency attribution (would-faster-help diagnostic) ------------------


def test_latency_attribution_clean_when_no_adverse_selection():
    # adverse selection ~0 at both horizons -> no pickoff, latency irrelevant.
    assert mm.latency_attribution(as_1s=-0.05, as_30s=-0.1) == "clean"


def test_latency_attribution_fast_pickoff_is_latency_fixable():
    # most of the 30s adverse selection already happened within 1s -> someone fast
    # hit our stale quote -> a faster system could have pulled it. Latency-fixable.
    assert mm.latency_attribution(as_1s=-8.0, as_30s=-10.0) == "fast_pickoff"


def test_latency_attribution_slow_drift_is_not_latency():
    # quote stayed good for 1s, the move bled in over the window -> fair-value/timing
    # problem, NOT latency. Faster wouldn't help.
    assert mm.latency_attribution(as_1s=-0.5, as_30s=-10.0) == "slow_drift"


# ----- daily runner alert decision (pure) ----------------------------------


def test_should_alert_only_on_a_gate_survivor():
    from scripts.research import edge_daily_run as edr
    survivors = [{"cell": ("60-89", "yes"), "settle_mean_net": 4.2}]
    assert edr.should_alert(survivors) is True
    assert edr.should_alert([]) is False
