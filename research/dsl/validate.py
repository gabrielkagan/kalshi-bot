"""Semantic validator for parsed Track A proposals.

`parse_proposal()` (schema.py) handles structural rejection — types,
enums, ranges. This module handles semantic rejection:

- Non-degeneracy: filters must actually filter something (not all
  axes None; ranges must have min ≤ max; subset lists must be
  non-empty).
- Hard kill switches: caller-supplied per-asset contract caps,
  global Kelly fraction ceiling. Non-negotiable per autoresearch
  design ("hard kill switches: 65% drawdown halt is non-negotiable;
  sizing must respect per-asset caps").
- Constraint references: caller declares which assets / regimes
  are registered; proposals referencing unregistered values are
  rejected here (not at schema level — the registered set varies
  by caller context).
- Cell-block hygiene: filter_stage_blocks entries must either be
  in KNOWN_BLOCK_STAGES (vendored from scripts/alpha_audit.py) or
  match the BLEED/DANGER/_blocked heuristic for forward-compat.

Returns `ValidationResult` (soft reject with reasons list) so
callers can log the rejection cause for yield-rate analysis.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import FrozenSet, List, Mapping

from research.dsl.schema import ParsedProposal


# Vendored from scripts/alpha_audit.py KNOWN_BLOCK_STAGES (sync-tested
# in research/tests/test_dsl_validate.py to catch oracle drift).
# Same vendor pattern as Ph1a's research/cell_blocks.py — when
# Ph1a + Ph1c.1 land on the same branch the canonical home is
# research/cell_blocks.py and this constant becomes a re-export.
KNOWN_BLOCK_STAGES = frozenset({
    "TM98_97_98C_2_5MIN_BLEED",
    "SOL_TAKER_85_89C_2_5MIN_BLEED",
    "96C_SOL_XRP_STC_DANGER_BAND",
    "tm96_calmlp_gate_blocked",
})


def looks_like_block_stage(stage: str) -> bool:
    """Forward-compat heuristic for cell-block stages not yet in
    KNOWN_BLOCK_STAGES. Matches alpha_audit.classify_stage's BLOCK-tier
    heuristic (BLEED/DANGER substring or _blocked suffix)."""
    return "BLEED" in stage or "DANGER" in stage or stage.endswith("_blocked")


@dataclass(frozen=True)
class Constraints:
    """Hard kill switches + registered-set declarations supplied by
    the caller. Track A and Track B inject these from their own
    context — research/dsl/ never imports bot/ to read them.

    `per_asset_cap`: max contracts per asset (mirrors live
    BTC_MAX_RISK_PER_TRADE-class limits). Missing entries → 0
    (proposal cannot trade that asset).
    `max_kelly_fraction`: global ceiling; proposals over this are
    rejected regardless of per-asset cap.
    `registered_assets`: superset that proposals' filters.asset must
    subset.
    `registered_regimes`: superset that proposals' filters.regime
    must subset (empirical set varies — May 5 snapshot saw
    `{normal, high, elevated, low, baseball, basketball, hockey,
    tennis, soccer}`).

    R1-finding-4: `frozen=True` does NOT make Constraints hashable
    because `per_asset_cap` is a Mapping and the canonical concrete
    (dict) is unhashable. Constraints is "frozen but not hashable
    by design" — callers who need a hashable key can derive one via
    `tuple(sorted(constraints.per_asset_cap.items()))`.
    """
    per_asset_cap: Mapping[str, int]
    max_kelly_fraction: float
    registered_assets: FrozenSet[str]
    registered_regimes: FrozenSet[str]


@dataclass(frozen=True)
class ValidationResult:
    accepted: bool
    reasons: List[str] = field(default_factory=list)


def validate(proposal: ParsedProposal,
             constraints: Constraints) -> ValidationResult:
    """Check non-degeneracy + kill switches + constraint references.
    `proposal` must already have passed `parse_proposal()` (i.e., be
    a ParsedProposal — structural validation done)."""
    reasons: List[str] = []

    # ── Non-degeneracy ───────────────────────────────────────────────
    f = proposal.filters
    if f.is_empty():
        reasons.append(
            "filters: every axis is omitted — proposal would replay "
            "every settled row (no-op filter)"
        )
    if f.asset is not None and len(f.asset) == 0:
        reasons.append("filters.asset: empty list — narrow-the-universe degenerate")
    if f.hour_of_day_utc is not None and len(f.hour_of_day_utc) == 0:
        reasons.append("filters.hour_of_day_utc: empty list")
    if f.day_of_week is not None and len(f.day_of_week) == 0:
        reasons.append("filters.day_of_week: empty list")
    if f.price_tier is not None and len(f.price_tier) == 0:
        reasons.append("filters.price_tier: empty list")
    if f.regime is not None and len(f.regime) == 0:
        reasons.append("filters.regime: empty list")
    if f.yes_ask_cents is not None:
        lo, hi = f.yes_ask_cents
        if lo > hi:
            reasons.append(
                f"filters.yes_ask_cents: min ({lo}) > max ({hi})"
            )
    if f.stc_seconds is not None:
        lo, hi = f.stc_seconds
        if lo > hi:
            reasons.append(
                f"filters.stc_seconds: min ({lo}) > max ({hi})"
            )

    # ── Constraint references (caller-declared registered sets) ─────
    if f.asset is not None:
        unregistered = sorted(set(f.asset) - constraints.registered_assets)
        if unregistered:
            reasons.append(
                f"filters.asset: contains unregistered asset(s) "
                f"{unregistered}; registered: "
                f"{sorted(constraints.registered_assets)}"
            )
    if f.regime is not None:
        unregistered = sorted(set(f.regime) - constraints.registered_regimes)
        if unregistered:
            reasons.append(
                f"filters.regime: contains unregistered regime(s) "
                f"{unregistered}; registered: "
                f"{sorted(constraints.registered_regimes)}"
            )

    # ── Hard kill switches ──────────────────────────────────────────
    s = proposal.sizing
    if s.fraction > constraints.max_kelly_fraction:
        reasons.append(
            f"sizing.fraction ({s.fraction}) > max_kelly_fraction "
            f"({constraints.max_kelly_fraction})"
        )
    # Per-asset cap check. If filters.asset is omitted, the proposal
    # could trade ANY registered asset — must respect every cap.
    candidate_assets = (
        list(f.asset) if f.asset is not None
        else sorted(constraints.registered_assets)
    )
    # R1-finding-2: empty candidate set means the proposal targets
    # NO tradeable asset (caller misconfigured constraints, or asset
    # filter is empty). Emit reason so the proposal doesn't silently
    # vacuous-pass the cap loop. R3-finding-2 / R2-finding-6: this
    # check precedes the cap loop, and the cap loop is INTENTIONALLY
    # a no-op when candidate_assets is empty — empty-set is reported
    # via the new reason here, not via cap_violations.
    if not candidate_assets:
        reasons.append(
            "sizing: no candidate assets to validate against — "
            "filters.asset is omitted AND constraints.registered_assets "
            "is empty, so the proposal targets nothing tradeable"
        )
    cap_violations = []
    for a in candidate_assets:
        cap = constraints.per_asset_cap.get(a)
        if cap is None:
            cap_violations.append(
                f"{a} (no cap declared in constraints; cannot verify)"
            )
        elif s.max_contracts > cap:
            cap_violations.append(f"{a} (cap={cap})")
    if cap_violations:
        reasons.append(
            f"sizing.max_contracts ({s.max_contracts}) exceeds "
            f"per-asset cap on: {cap_violations}"
        )

    # ── Cell-block hygiene ──────────────────────────────────────────
    unknown_blocks: List[str] = []
    for stage in proposal.exclusions.filter_stage_blocks:
        if stage in KNOWN_BLOCK_STAGES:
            continue
        if looks_like_block_stage(stage):
            continue
        unknown_blocks.append(stage)
    if unknown_blocks:
        reasons.append(
            f"exclusions.filter_stage_blocks: unknown stage(s) "
            f"{unknown_blocks}; not in KNOWN_BLOCK_STAGES "
            f"{sorted(KNOWN_BLOCK_STAGES)} and don't match "
            "BLEED/DANGER/_blocked heuristic — likely typo"
        )

    return ValidationResult(accepted=not reasons, reasons=reasons)
