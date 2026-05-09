"""Semantic validation tests for research.dsl.validate."""
from __future__ import annotations

from research.dsl.schema import parse_proposal
from research.dsl.validate import (
    KNOWN_BLOCK_STAGES, Constraints, ValidationResult,
    looks_like_block_stage, validate,
)


# ── Helpers ────────────────────────────────────────────────────────────


def _default_constraints(**overrides):
    base = dict(
        per_asset_cap={"BTC": 50, "ETH": 50, "SOL": 100, "XRP": 100},
        max_kelly_fraction=0.5,
        registered_assets=frozenset({"BTC", "ETH", "SOL", "XRP"}),
        registered_regimes=frozenset({"normal", "high", "elevated", "low"}),
    )
    base.update(overrides)
    return Constraints(**base)


def _proposal(yaml_text):
    return parse_proposal(yaml_text)


VALID = """
filters:
  asset: ["BTC"]
  yes_ask_cents: [80, 99]
sizing:
  rule: "kelly_capped"
  fraction: 0.25
  max_contracts: 50
  stc_scaler: false
"""


# ── Accept ─────────────────────────────────────────────────────────────


def test_accept_valid_proposal():
    r = validate(_proposal(VALID), _default_constraints())
    assert r.accepted is True
    assert r.reasons == []


def test_accept_full_proposal():
    yml = """
filters:
  asset: ["BTC", "ETH"]
  yes_ask_cents: [85, 99]
  edge_threshold: 0.02
  stc_seconds: [60, 300]
  hour_of_day_utc: [13, 14, 15]
  day_of_week: ["mon", "wed", "fri"]
  price_tier: ["90-97"]
  regime: ["normal"]
sizing:
  rule: "kelly_capped"
  fraction: 0.25
  max_contracts: 30
  stc_scaler: true
exclusions:
  filter_stage_blocks: ["TM98_97_98C_2_5MIN_BLEED"]
"""
    r = validate(_proposal(yml), _default_constraints())
    assert r.accepted is True, r.reasons


# ── Non-degeneracy ─────────────────────────────────────────────────────


def test_reject_no_filter_axes():
    yml = """
filters: {}
sizing:
  rule: "kelly_capped"
  fraction: 0.25
  max_contracts: 50
  stc_scaler: false
"""
    r = validate(_proposal(yml), _default_constraints())
    assert r.accepted is False
    assert any("every axis is omitted" in s for s in r.reasons)


def test_reject_empty_asset_list():
    yml = """
filters:
  asset: []
sizing:
  rule: "kelly_capped"
  fraction: 0.25
  max_contracts: 50
  stc_scaler: false
"""
    r = validate(_proposal(yml), _default_constraints())
    assert r.accepted is False
    assert any("filters.asset" in s for s in r.reasons)


def test_reject_yes_ask_min_gt_max():
    yml = """
filters:
  asset: ["BTC"]
  yes_ask_cents: [99, 80]
sizing:
  rule: "kelly_capped"
  fraction: 0.25
  max_contracts: 50
  stc_scaler: false
"""
    r = validate(_proposal(yml), _default_constraints())
    assert r.accepted is False
    assert any("yes_ask_cents" in s and "min" in s for s in r.reasons)


def test_reject_stc_seconds_min_gt_max():
    yml = """
filters:
  asset: ["BTC"]
  stc_seconds: [600, 60]
sizing:
  rule: "kelly_capped"
  fraction: 0.25
  max_contracts: 50
  stc_scaler: false
"""
    r = validate(_proposal(yml), _default_constraints())
    assert r.accepted is False
    assert any("stc_seconds" in s and "min" in s for s in r.reasons)


# ── Constraint references ──────────────────────────────────────────────


def test_reject_unregistered_asset():
    # Schema rejects unknown assets like DOGE at parse time (they're
    # not in the canonical ASSETS set in schema.py). Validate's
    # `registered_assets` constraint adds a SECOND axis: caller can
    # restrict the registered set to a subset of the canonical assets,
    # so a proposal referencing ETH gets rejected when caller only
    # allows BTC.
    yml2 = """
filters:
  asset: ["BTC", "ETH"]
sizing:
  rule: "kelly_capped"
  fraction: 0.25
  max_contracts: 50
  stc_scaler: false
"""
    constraints = _default_constraints(
        registered_assets=frozenset({"BTC"}),
    )
    r = validate(_proposal(yml2), constraints)
    assert r.accepted is False
    assert any("unregistered asset" in s for s in r.reasons)


def test_reject_unregistered_regime():
    yml = """
filters:
  asset: ["BTC"]
  regime: ["normal", "exotic_regime"]
sizing:
  rule: "kelly_capped"
  fraction: 0.25
  max_contracts: 50
  stc_scaler: false
"""
    r = validate(_proposal(yml), _default_constraints())
    assert r.accepted is False
    assert any("unregistered regime" in s for s in r.reasons)


# ── Hard kill switches ─────────────────────────────────────────────────


def test_reject_max_contracts_over_per_asset_cap():
    yml = """
filters:
  asset: ["BTC"]
sizing:
  rule: "kelly_capped"
  fraction: 0.25
  max_contracts: 100
  stc_scaler: false
"""
    r = validate(
        _proposal(yml),
        _default_constraints(per_asset_cap={"BTC": 50}),
    )
    assert r.accepted is False
    assert any("max_contracts" in s and "BTC" in s for s in r.reasons)


def test_reject_kelly_fraction_over_global_ceiling():
    yml = """
filters:
  asset: ["BTC"]
sizing:
  rule: "kelly_capped"
  fraction: 0.7
  max_contracts: 50
  stc_scaler: false
"""
    r = validate(
        _proposal(yml),
        _default_constraints(max_kelly_fraction=0.5),
    )
    assert r.accepted is False
    assert any("max_kelly_fraction" in s for s in r.reasons)


def test_no_asset_filter_means_all_caps_must_pass():
    """When filters.asset is omitted the proposal could trade any
    registered asset. Cap must respect EVERY one."""
    yml = """
filters:
  yes_ask_cents: [80, 99]
sizing:
  rule: "kelly_capped"
  fraction: 0.25
  max_contracts: 75
  stc_scaler: false
"""
    constraints = _default_constraints(
        per_asset_cap={"BTC": 50, "ETH": 50, "SOL": 100, "XRP": 100},
    )
    r = validate(_proposal(yml), constraints)
    assert r.accepted is False
    assert any("BTC" in s and "ETH" in s for s in r.reasons)


def test_per_asset_cap_missing_entry_rejects():
    """Missing cap entry means we cannot verify — reject loudly
    rather than fall through to a default."""
    yml = """
filters:
  asset: ["BTC", "SOL"]
sizing:
  rule: "kelly_capped"
  fraction: 0.25
  max_contracts: 50
  stc_scaler: false
"""
    constraints = _default_constraints(
        per_asset_cap={"BTC": 50},   # SOL missing
    )
    r = validate(_proposal(yml), constraints)
    assert r.accepted is False
    assert any("no cap declared" in s for s in r.reasons)


# ── Cell-block hygiene ─────────────────────────────────────────────────


def test_accept_known_block_stage():
    yml = VALID + "\nexclusions:\n  filter_stage_blocks: ['TM98_97_98C_2_5MIN_BLEED']\n"
    r = validate(_proposal(yml), _default_constraints())
    assert r.accepted is True


def test_accept_heuristic_match_unknown_block():
    """Stage not in KNOWN_BLOCK_STAGES but matches BLEED/DANGER
    heuristic should pass (forward-compat)."""
    yml = VALID + "\nexclusions:\n  filter_stage_blocks: ['SPECULATIVE_FUTURE_BLEED']\n"
    r = validate(_proposal(yml), _default_constraints())
    assert r.accepted is True


def test_reject_typo_block_stage():
    """Catches typos like TM98_BLED instead of TM98_BLEED."""
    yml = VALID + "\nexclusions:\n  filter_stage_blocks: ['TM98_BLED']\n"
    r = validate(_proposal(yml), _default_constraints())
    assert r.accepted is False
    assert any("filter_stage_blocks" in s for s in r.reasons)


def test_looks_like_block_stage_heuristic():
    assert looks_like_block_stage("ANY_BLEED") is True
    assert looks_like_block_stage("DANGER_ZONE") is True
    assert looks_like_block_stage("foo_blocked") is True
    assert looks_like_block_stage("regular_filter") is False
    assert looks_like_block_stage("") is False


# ── ValidationResult shape ─────────────────────────────────────────────


def test_validation_result_accepted_when_no_reasons():
    r = ValidationResult(accepted=True, reasons=[])
    assert r.accepted is True


def test_validation_aggregates_multiple_reasons():
    """A proposal with multiple defects should report all of them,
    not short-circuit on the first."""
    yml = """
filters:
  asset: []
  yes_ask_cents: [99, 80]
sizing:
  rule: "kelly_capped"
  fraction: 0.7
  max_contracts: 100
  stc_scaler: false
"""
    r = validate(
        _proposal(yml),
        _default_constraints(
            per_asset_cap={"BTC": 50, "ETH": 50, "SOL": 100, "XRP": 100},
            max_kelly_fraction=0.5,
        ),
    )
    assert r.accepted is False
    # Expect at least 3 distinct reasons: empty asset, yes_ask min>max,
    # fraction over ceiling.
    assert len(r.reasons) >= 3


# ── Sync test: KNOWN_BLOCK_STAGES parity vs alpha_audit ────────────────


# ── R1 regression tests ───────────────────────────────────────────────


def test_r1_finding_2_empty_registered_assets_with_no_filter_rejects():
    """Empty registered_assets + omitted filters.asset means the
    proposal targets nothing tradeable. Validator must reject loudly
    rather than vacuous-pass the cap loop."""
    yml = """
filters:
  yes_ask_cents: [80, 99]
sizing:
  rule: "kelly_capped"
  fraction: 0.25
  max_contracts: 50
  stc_scaler: false
"""
    constraints = _default_constraints(
        registered_assets=frozenset(),
        per_asset_cap={},
    )
    r = validate(_proposal(yml), constraints)
    assert r.accepted is False
    assert any("targets nothing tradeable" in s for s in r.reasons)


def test_known_block_stages_parity_with_alpha_audit():
    """research.dsl.validate.KNOWN_BLOCK_STAGES must stay in sync
    with scripts/alpha_audit.py KNOWN_BLOCK_STAGES (the canonical
    oracle). When Ph1a + Ph1c.1 land on the same branch this test
    will move to the consolidated research/cell_blocks.py."""
    import os
    import sys
    here = os.path.dirname(os.path.abspath(__file__))
    scripts_dir = os.path.normpath(os.path.join(here, "..", "..", "scripts"))
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    import alpha_audit   # noqa: E402  (sys.path injected just above)
    assert KNOWN_BLOCK_STAGES == alpha_audit.KNOWN_BLOCK_STAGES
