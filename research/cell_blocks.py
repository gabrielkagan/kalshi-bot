"""Stage classification — vendored from scripts/alpha_audit.py.

Why vendored, not imported: research/ is a leaf in the import graph.
scripts/alpha_audit.py imports nothing from research/, but if the
ph1a harness imports from scripts/, every Track A / Track B sweep
job pulls scripts/alpha_audit.py's stdlib + sqlite3 module-level
state. Independence is the cleaner contract.

Parity with the alpha_audit oracle is enforced by
research/tests/test_cell_blocks_sync.py — any drift in the live
constants WILL fail the parity test. Update both in lock-step.
"""
from __future__ import annotations

from typing import Optional


HARD_REJECT_STAGES = frozenset({
    # Pre-evaluation hard stops.
    "insufficient_edge", "price_out_of_range", "zero_sizing",
    "silent_loss_cooldown", "silent_vol_none", "silent_spot_none",
    "dead_hour_passed",
    "low_probability", "no_best_ask", "no_orderbook",
    "overnight_lp_vol_skip", "single_asset_selection",
    "strategy_wait", "threshold_implausible",
})

KNOWN_BLOCK_STAGES = frozenset({
    "TM98_97_98C_2_5MIN_BLEED",
    "SOL_TAKER_85_89C_2_5MIN_BLEED",
    "96C_SOL_XRP_STC_DANGER_BAND",
    "tm96_calmlp_gate_blocked",
})

EXPLICIT_STAGE_CLASSIFICATIONS = {
    "terminal_momentum": "SHADOW",
    "weekend_discount": "SHADOW",
    "overnight_discount": "SHADOW",
    "sol_usmorn_sub88": "SHADOW",
    "sol_low_entry_high_stc": "SHADOW",
    "usaft_short_stc": "HARD_REJECT",
    "hourly_live": "CANDIDATE",
}


def classify_stage(stage: Optional[str]) -> str:
    """Tier for a filter_stage value.

    Heuristic mirrors alpha_audit.classify_stage so the cell-block
    UNION pattern (CLAUDE.md) deflates candidate volume identically
    in research as in alpha_audit.
    """
    if not stage:
        return "UNKNOWN"
    if stage == "candidate":
        return "CANDIDATE"
    if stage in EXPLICIT_STAGE_CLASSIFICATIONS:
        return EXPLICIT_STAGE_CLASSIFICATIONS[stage]
    if stage in HARD_REJECT_STAGES:
        return "HARD_REJECT"
    if stage in KNOWN_BLOCK_STAGES:
        return "BLOCK"
    s = stage
    if (s.endswith("_shadow")
            or s.startswith("decided_contract")
            or s.startswith("dc_shadow")
            or s.startswith("dc_t")
            or "shadow" in s.lower()):
        return "SHADOW"
    if "BLEED" in s or "DANGER" in s or s.endswith("_blocked"):
        return "BLOCK"
    return "UNKNOWN"


def is_win(side: Optional[str], market_result: Optional[str]) -> bool:
    """Side-aware win check. NO opps win on 'no'/'all_no'."""
    s = (side or "yes").lower()
    r = (market_result or "").lower()
    if s == "no":
        return r in ("no", "all_no")
    return r in ("yes", "all_yes")
