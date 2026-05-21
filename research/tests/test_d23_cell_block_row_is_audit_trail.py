"""D-23 — cell-block stage IS the rejection row (audit trail with stored cf).

Authoritative source: bot._impl branch insertions for filter_stage='<BLOCK_STAGE>'
(per RCA D-23). When a cell-block fires, the row gets `filter_stage=<BLOCK>`
INSTEAD OF 'candidate', but still carries `position_size`, `calibrated_prob`,
etc. — including a fully-computed `counterfactual_pnl` because settlement
tracks block rows like candidate rows. The `order_id` field is NULL because
no order was ever submitted.

The replay engine's "would-have-traded under config-with-block-disabled"
predicate must use the row's stored cf_pnl UNMODIFIED. Replay does NOT
recompute cf for block rows; it just consults the stored value.

D-23 is a contract pin on `replay_cf_pnl`'s OBLIVIOUSNESS to filter_stage:
the function does NOT take filter_stage as input. The "would-have-traded"
predicate (a separate function — B3 ships) consults filter_stage; cf is
computed identically.
"""
from __future__ import annotations

import inspect

import pytest

from research.replay import replay_cf_pnl, replay_taker_fee


def test_d23_replay_cf_pnl_signature_does_not_take_filter_stage() -> None:
    """Pin the contract: replay_cf_pnl does NOT branch on filter_stage.

    Per RCA D-23: "Cf for the 'would have traded' case: row's stored cf_pnl is
    already computed (cell-block rows are settlement-tracked just like
    'candidate' rows) — replay can use it directly."

    If someone refactors replay_cf_pnl to accept a filter_stage parameter,
    that signals a semantic shift that should be reviewed before landing.
    """
    sig = inspect.signature(replay_cf_pnl)
    assert "filter_stage" not in sig.parameters, (
        f"D-23 signature drift: replay_cf_pnl gained filter_stage param. "
        f"Block rows must compute identically to candidate rows. "
        f"Signature: {sig}"
    )


@pytest.mark.parametrize("block_stage", [
    "96C_SOL_XRP_STC_DANGER_BAND",
    "TM98_97_98C_2_5MIN_BLEED",
    "SOL_TAKER_85_89C_2_5MIN_BLEED",
    "tm96_calmlp_gate_blocked",
])
def test_d23_block_row_cf_identical_to_candidate(block_stage: str) -> None:
    """A synthetic block row's cf is identical to the same row's cf if it had been a candidate.

    Per RCA D-23 example: block row with position_size=10, result=YES, entry=96,
    side=YES → cf = (100-96)*10 - taker_fee(10, 96).
    """
    # Two identical-input cf calculations, simulating
    # (a) the row stored as 'candidate' and (b) the row stored as <block_stage>.
    # Since replay_cf_pnl doesn't take filter_stage, the two paths are by
    # construction the same call. This documents the contract.
    cf_candidate_path = replay_cf_pnl(
        entry_price=96,
        market_result="yes",
        side="yes",
        position_size=10,
        product_type="15m",
    )
    # The 'block_stage' in this loop is documentary — replay_cf_pnl doesn't see it.
    cf_block_path = replay_cf_pnl(
        entry_price=96,
        market_result="yes",
        side="yes",
        position_size=10,
        product_type="15m",
    )
    expected = (100 - 96) * 10 - replay_taker_fee(10, 96)
    # 0.07 * 10 * 96 * 4 / 100 = 2.688 -> ceil = 3. (100-96)*10 - 3 = 37.
    assert cf_candidate_path == cf_block_path == expected, (
        f"D-23 block-stage={block_stage!r}: candidate_cf={cf_candidate_path} "
        f"block_cf={cf_block_path} expected={expected}"
    )
    assert expected == 37, f"D-23 RCA example: {expected} vs 37"


def test_d23_block_row_with_loss_still_computes_normally() -> None:
    """Block row that would have been a LOSS still produces signed cf via canonical formula."""
    cf = replay_cf_pnl(
        entry_price=96,
        market_result="no",
        side="yes",
        position_size=10,
        product_type="15m",
    )
    # LOSS @ 96, 10ct: -(96*10 + fee(10,96)) = -(960 + 3) = -963
    expected = -(96 * 10 + replay_taker_fee(10, 96))
    assert cf == expected == -963, (
        f"D-23 block-row LOSS: expected {expected} (=-963), got {cf}"
    )


def test_d23_block_row_with_position_size_null_uses_1ct_default() -> None:
    """Block rows with NULL position_size still use the 1ct hybrid (D-7).

    The 1ct hybrid is at the cf computation layer, not gated on filter_stage.
    Pin that block rows inherit D-7's NULL-defaults behavior.
    """
    cf = replay_cf_pnl(
        entry_price=96,
        market_result="yes",
        side="yes",
        position_size=None,    # NULL block-row size
        product_type="15m",
    )
    # 1ct win @ 96: (100-96)*1 - ceil(0.07*1*96*4/100) = 4 - 1 = 3
    assert cf == 3, f"D-23 block-row NULL-size: expected 3 (1ct), got {cf}"


def test_d23_block_row_classification_is_separate_from_cf() -> None:
    """Replay's cf path is decoupled from the would-have-traded predicate.

    The "would-have-traded" call site (filter_stage → True/False under modified
    config) is a SEPARATE function that B3 ships. cf is precomputed and stored.
    Pin the architecture: cf doesn't know about filter_stage; the predicate
    doesn't know about cf math.

    This is a "function-existence absence" test: replay_cf_pnl's local namespace
    does not reference filter_stage strings.
    """
    src = inspect.getsource(replay_cf_pnl)
    forbidden = [
        "filter_stage",
        "BLEED",
        "DANGER",
        "_blocked",
        "calmlp_gate",
    ]
    for needle in forbidden:
        assert needle not in src, (
            f"D-23 cf/predicate coupling: replay_cf_pnl source mentions {needle!r}. "
            f"cf computation must not depend on filter_stage classification."
        )
