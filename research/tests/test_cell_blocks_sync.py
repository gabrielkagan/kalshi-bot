"""Pin research.cell_blocks against scripts.alpha_audit.

research/ vendors the cell-block constants instead of importing them
to keep research/ a leaf in the import graph (autoresearch design
isolation rules). This test catches drift on either side: any change
to alpha_audit's classification surface that isn't ported to research
fails here in the same PR.
"""
import alpha_audit  # noqa: F401  (sys.path injected by conftest.py)

from research import cell_blocks


def test_known_block_stages_parity():
    assert cell_blocks.KNOWN_BLOCK_STAGES == alpha_audit.KNOWN_BLOCK_STAGES


def test_hard_reject_stages_parity():
    assert cell_blocks.HARD_REJECT_STAGES == alpha_audit.HARD_REJECT_STAGES


def test_explicit_classifications_parity():
    assert (
        cell_blocks.EXPLICIT_STAGE_CLASSIFICATIONS
        == alpha_audit.EXPLICIT_STAGE_CLASSIFICATIONS
    )


def test_classify_stage_parity_across_known_inputs():
    """Heuristic parity matters as much as constants — drift in the
    suffix/substring rules silently re-tiers stages."""
    inputs = [
        None, "", "candidate",
        "TM98_97_98C_2_5MIN_BLEED",       # KNOWN_BLOCK
        "tm96_calmlp_gate_blocked",       # KNOWN_BLOCK
        "no_side_price_shadow_xrp",       # _shadow suffix
        "low_price_shadow",               # _shadow suffix
        "decided_contract_t0_z0",         # decided_contract prefix
        "dc_shadow_t2_90c",               # dc_shadow prefix
        "dc_t1_z2",                       # dc_t prefix
        "MYSTERY_BLEED_NEW",              # BLEED substring → BLOCK
        "DANGER_BAND_FUTURE",             # DANGER substring → BLOCK
        "experimental_blocked",           # _blocked suffix → BLOCK
        "insufficient_edge",              # HARD_REJECT
        "terminal_momentum",              # EXPLICIT → SHADOW
        "usaft_short_stc",                # EXPLICIT → HARD_REJECT
        "hourly_live",                    # EXPLICIT → CANDIDATE
        "unknown_future_tag",             # heuristic → UNKNOWN
    ]
    for i in inputs:
        assert cell_blocks.classify_stage(i) == alpha_audit.classify_stage(i), (
            f"drift on input {i!r}: research={cell_blocks.classify_stage(i)} "
            f"vs alpha_audit={alpha_audit.classify_stage(i)}"
        )


def test_is_win_parity():
    cases = [
        ("yes", "yes"), ("yes", "no"), ("yes", "all_yes"), ("yes", "all_no"),
        ("no", "yes"), ("no", "no"), ("no", "all_yes"), ("no", "all_no"),
        (None, "yes"), (None, "no"),  # legacy NULL → defaults YES
        ("YES", "Yes"), ("NO", "NO"),  # case-insensitivity
        ("yes", None), (None, None), ("yes", ""),
    ]
    for side, result in cases:
        assert (
            cell_blocks.is_win(side, result)
            == alpha_audit.is_win(side, result)
        ), f"drift on ({side!r}, {result!r})"
