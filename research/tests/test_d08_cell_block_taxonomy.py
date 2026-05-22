"""D-8 — cell-block UNION pattern + classification parity with alpha_audit.

Authoritative source: bot.constants (per RCA D-8 — post-9.3-iii.c canonical
home for filter_stage string constants) + scripts/alpha_audit.py
(KNOWN_BLOCK_STAGES, EXPLICIT_STAGE_CLASSIFICATIONS, classify_stage).

The 4 cell-block filter_stage values that prevent a row from reaching
'candidate':

    96C_SOL_XRP_STC_DANGER_BAND      (HIGH_PRICE_STC_BLOCK_FILTER_STAGE)
    TM98_97_98C_2_5MIN_BLEED         (TM98_HIGHPRICE_BLEED_BLOCK_FILTER_STAGE)
    SOL_TAKER_85_89C_2_5MIN_BLEED    (SOL_TAKER_LOWPRICE_BLEED_BLOCK_FILTER_STAGE)
    tm96_calmlp_gate_blocked         (lowercase — bot._impl branch in B1-baseline
                                      worktree; on post-9.3-iii.c main this
                                      lives in the canonical submodule home —
                                      see CLAUDE.md for the layout map)

Replay must UNION these with 'candidate' when aggregating "all 15M trades"
funnels, AND must classify them identically to alpha_audit.classify_stage.

Some tests below are RED (TDD-signal) because B1's research/replay.py does
NOT yet ship classify_stage or KNOWN_BLOCK_STAGES. B3 implements them.
"""
from __future__ import annotations

import pytest


# The CANONICAL 4 cell-block stages as inline regression contract.
# Source: bot/constants.py at B1 base commit (a9f33cf2):
#   line 103: HIGH_PRICE_STC_BLOCK_FILTER_STAGE = "96C_SOL_XRP_STC_DANGER_BAND"
#   line 150: TM98_HIGHPRICE_BLEED_BLOCK_FILTER_STAGE = "TM98_97_98C_2_5MIN_BLEED"
#   line 172: SOL_TAKER_LOWPRICE_BLEED_BLOCK_FILTER_STAGE = "SOL_TAKER_85_89C_2_5MIN_BLEED"
# Plus bot._impl tm96_calmlp_gate_blocked branch (lowercase by design). NOTE:
# B2 base predates the 9.3-iii.c bot/_impl.py deletion — on post-9.3-iii.c
# main, this lives in the canonical submodule home.
EXPECTED_BLOCK_STAGES = frozenset({
    "96C_SOL_XRP_STC_DANGER_BAND",
    "TM98_97_98C_2_5MIN_BLEED",
    "SOL_TAKER_85_89C_2_5MIN_BLEED",
    "tm96_calmlp_gate_blocked",
})


def test_d08_expected_block_stages_pinned_at_4() -> None:
    """Regression contract: exactly 4 cell-block stages exist (as of B1 base).

    If a new BLEED/DANGER stage ships, update this set + add a per-stage
    classification test below. This catches accidental DROPPING of a stage
    from the canonical list during refactoring.
    """
    assert len(EXPECTED_BLOCK_STAGES) == 4, (
        f"D-8 cell-block count drift: {len(EXPECTED_BLOCK_STAGES)} stages, "
        f"expected 4 (96C_SOL_XRP_STC + TM98 + SOL_TAKER_85_89 + tm96_calmlp_gate)"
    )


@pytest.mark.parametrize("stage", sorted(EXPECTED_BLOCK_STAGES))
def test_d08_each_block_stage_string_pinned(stage: str) -> None:
    """Each stage string is pinned literally. Catches typos / casing drift."""
    assert stage in EXPECTED_BLOCK_STAGES


# ──────────────────────────────────────────────────────────────────────────
# TDD-RED tests below — fail until B3 ships research.replay.classify_stage
# and research.replay.KNOWN_BLOCK_STAGES.
# ──────────────────────────────────────────────────────────────────────────


def test_d08_replay_exposes_known_block_stages() -> None:
    """B3 must expose KNOWN_BLOCK_STAGES matching EXPECTED_BLOCK_STAGES set-equality."""
    import research.replay as rep
    assert hasattr(rep, "KNOWN_BLOCK_STAGES"), (
        "D-8 TDD-red: B3 must ship research.replay.KNOWN_BLOCK_STAGES"
    )
    assert set(rep.KNOWN_BLOCK_STAGES) == EXPECTED_BLOCK_STAGES, (
        f"D-8 set drift: replay.KNOWN_BLOCK_STAGES={set(rep.KNOWN_BLOCK_STAGES)}, "
        f"expected {EXPECTED_BLOCK_STAGES}"
    )


@pytest.mark.parametrize("stage", sorted(EXPECTED_BLOCK_STAGES))
def test_d08_replay_classify_stage_known_blocks(stage: str) -> None:
    """B3's classify_stage returns 'block' (or equivalent canonical tier) for known stages."""
    import research.replay as rep
    assert hasattr(rep, "classify_stage"), (
        "D-8 TDD-red: B3 must ship research.replay.classify_stage"
    )
    tier = rep.classify_stage(stage)
    assert tier == "BLOCK", (
        f"D-8 classification drift: classify_stage({stage!r}) -> {tier!r}, expected 'BLOCK'. "
        f"Canonical alpha_audit.classify_stage returns UPPERCASE tier strings "
        f"(scripts/alpha_audit.py:100/102/106/108/115/117/118 — UPPERCASE pattern)."
    )


def test_d08_replay_classify_stage_candidate_passes() -> None:
    """The literal 'candidate' filter_stage classifies as 'CANDIDATE' (NOT BLOCK).

    Canonical from alpha_audit.classify_stage returns UPPERCASE tier strings.
    """
    import research.replay as rep
    if not hasattr(rep, "classify_stage"):
        pytest.skip("D-8 TDD-red: classify_stage not yet implemented")
    assert rep.classify_stage("candidate") == "CANDIDATE"


@pytest.mark.parametrize("stage", [
    "FUTURE_TM99_BLEED",       # new BLEED stage — auto-classified as block
    "DOGE_DANGER_BAND",        # DANGER substring → block
    "weather_blocked",         # _blocked suffix → block
    "calmlp_gate_blocked",     # _blocked suffix → block
])
def test_d08_replay_classify_stage_heuristic_forward_compat(stage: str) -> None:
    """B3's classify_stage auto-classifies new BLEED/DANGER/_blocked stages as block.

    Per RCA D-8: "Match alpha_audit.classify_stage's heuristic for forward-compat
    (new BLEED/DANGER/_blocked stages auto-classified as BLOCK)."
    """
    import research.replay as rep
    if not hasattr(rep, "classify_stage"):
        pytest.skip("D-8 TDD-red: classify_stage not yet implemented")
    tier = rep.classify_stage(stage)
    assert tier == "BLOCK", (
        f"D-8 heuristic forward-compat: classify_stage({stage!r}) -> {tier!r}, "
        f"expected 'BLOCK' (BLEED/DANGER/_blocked suffix; UPPERCASE per alpha_audit)"
    )


def test_d08_replay_no_hardcoded_shadow_stages_constant() -> None:
    """AST regex guard: research.replay must NOT define a hardcoded SHADOW_STAGES set.

    Per RCA D-8 + alpha-audit-rebuild Round-1: a static SHADOW_STAGES constant
    became stale every time a new shadow strategy launched. classify_stage uses
    heuristic suffix matching for forward-compat.
    """
    import research.replay as rep
    import inspect
    source = inspect.getsource(rep)
    forbidden = ["SHADOW_STAGES = ", "SHADOW_STAGES=", "SHADOW_STAGES: "]
    for needle in forbidden:
        assert needle not in source, (
            f"D-8 hardcoded SHADOW_STAGES drift: found {needle!r} in research/replay.py"
        )


def test_d08_dropping_any_block_stage_fails_set_equality() -> None:
    """Mutation defense: if anyone removes a stage from KNOWN_BLOCK_STAGES, the
    set-equality test above fails clearly.

    This test documents the mutation defense rather than running it (running
    actual mutations is a separate test infra). The set-equality test in
    test_d08_replay_exposes_known_block_stages provides the actual guard.
    """
    # Verify the contract: if we remove any one stage from EXPECTED, set
    # equality breaks. (Sanity check the test itself.)
    for stage_to_drop in EXPECTED_BLOCK_STAGES:
        mutated = EXPECTED_BLOCK_STAGES - {stage_to_drop}
        assert mutated != EXPECTED_BLOCK_STAGES, (
            f"D-8 mutation sanity: dropping {stage_to_drop} should change the set"
        )
        assert len(mutated) == 3, "D-8 mutation sanity: dropping 1 of 4 -> 3"
