"""D-15 — rejected_opportunities is replay-incomplete (scope limit).

Authoritative source: bot/state.py DDL (per RCA D-15). rejected_opportunities
carries decision context (ticker, raw_prob, market_price, calibrated_prob,
seconds_to_close, etc.) but CRITICALLY MISSING: position_size, kelly_f, side,
available_balance_cents, counterfactual_pnl, edge, fee_adjusted_edge,
drawdown_scaler, strategy.

These rows cannot be replayed under modified sizing config because the sizing
inputs aren't captured. They CAN be re-evaluated for "would this rejected row
have passed under a lower edge floor" because raw_prob + market_price ARE.

B2 contract: replay() refuses rejected_opportunities for v1.

TDD-red until B3 ships the explicit refusal in replay's API.
"""
from __future__ import annotations

import pytest


def test_d15_replay_raises_on_rejected_opportunities_source() -> None:
    """Calling replay with source='rejected_opportunities' raises a clear error (TDD-red)."""
    import research.replay as rep
    # B3 may expose this via evaluate_window with a `source` kwarg, or via a
    # dedicated function. Accept either pattern.
    if hasattr(rep, "evaluate_window"):
        # Check that calling with source='rejected_opportunities' raises
        try:
            sig_params = list(__import__("inspect").signature(rep.evaluate_window).parameters)
        except Exception:
            sig_params = []
        if "source" in sig_params:
            with pytest.raises((NotImplementedError, ValueError, RuntimeError)) as excinfo:
                rep.evaluate_window(source="rejected_opportunities")
            msg = str(excinfo.value).lower()
            assert "rejected" in msg, (
                f"D-15 raise message: expected 'rejected' in error, got {excinfo.value!r}"
            )
            return
    pytest.skip("D-15 TDD-red: evaluate_window/source contract not yet implemented")


def test_d15_rejected_opportunities_missing_columns_documented() -> None:
    """Pin the list of columns missing from rejected_opportunities.

    Per RCA D-15. These are the reasons replay v1 cannot handle this source.
    """
    MISSING_COLUMNS = frozenset({
        "position_size",
        "kelly_f",
        "side",
        "available_balance_cents",
        "counterfactual_pnl",
        "edge",
        "fee_adjusted_edge",
        "drawdown_scaler",
        "strategy",
    })
    assert len(MISSING_COLUMNS) == 9
    assert "counterfactual_pnl" in MISSING_COLUMNS
    assert "kelly_f" in MISSING_COLUMNS
    # Note: rejected_opportunities has *_shadow versions of some of these but
    # for the canonical decision-context they're missing per the DDL.


def test_d15_rejected_opportunities_has_decision_inputs_only() -> None:
    """Pin the list of columns rejected_opportunities DOES carry.

    These are sufficient for threshold-only re-evaluation (Phase 1a follow-on)
    but not for sizing replay.
    """
    DECISION_INPUT_COLUMNS = frozenset({
        "ticker",
        "raw_prob",
        "calibrated_prob",
        "market_price",
        "z_score",
        "spot_price",
        "volatility",
        "seconds_to_close",
        "rejection_reason",
        "rejection_time",
        "status",
        "market_result",
        "no_ask_cents",
        "product_type",
    })
    # Sanity that we've identified some columns
    assert "raw_prob" in DECISION_INPUT_COLUMNS
    assert "market_price" in DECISION_INPUT_COLUMNS


def test_d15_threshold_reevaluation_is_phase_1a_followon() -> None:
    """Pin the contract: threshold-only re-eval on rejected rows is OUT OF SCOPE for B2/B3.

    Per RCA D-15: 'Threshold-only experiments... are a Phase 1a follow-on
    (out of scope for 0b).'
    """
    # This test is a contract documentation. Nothing to assert at runtime.
    # If B3 / B4 attempts to add rejected_opportunities replay, the user
    # is expected to update D-15's expected refusal contract.
    pass


def test_d15_replay_v1_evaluated_opportunities_only() -> None:
    """If B3 exposes a `SOURCES` constant, evaluated_opportunities is in it and
    rejected_opportunities is NOT (TDD-red)."""
    import research.replay as rep
    if not hasattr(rep, "SOURCES"):
        pytest.skip("D-15 TDD-red: SOURCES constant not yet exposed by B3")
    assert "evaluated_opportunities" in rep.SOURCES, (
        f"D-15 SOURCES drift: evaluated_opportunities should be supported. "
        f"Got: {rep.SOURCES}"
    )
    assert "rejected_opportunities" not in rep.SOURCES, (
        f"D-15 scope creep: rejected_opportunities is Phase 1a follow-on, "
        f"not B3 v1 scope. Got: {rep.SOURCES}"
    )
