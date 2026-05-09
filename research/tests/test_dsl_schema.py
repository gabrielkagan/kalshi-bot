"""Structural-parse tests for research.dsl.schema."""
from __future__ import annotations

import pytest

from research.dsl.schema import (
    ParsedProposal, SchemaError, parse_proposal,
)


# ── Valid parses ───────────────────────────────────────────────────────


VALID_MINIMAL = """
filters:
  asset: ["BTC"]
sizing:
  rule: "kelly_capped"
  fraction: 0.25
  max_contracts: 50
  stc_scaler: false
"""

VALID_FULL = """
thesis: "BTC 90-97c with mid-day STC + normal regime"
filters:
  asset: ["BTC", "ETH"]
  yes_ask_cents: [80, 99]
  edge_threshold: 0.015
  stc_seconds: [60, 300]
  hour_of_day_utc: [13, 14, 15, 16]
  day_of_week: ["mon", "tue", "wed", "thu", "fri"]
  price_tier: ["90-97", "98-99"]
  regime: ["normal", "high"]
sizing:
  rule: "kelly_capped"
  fraction: 0.25
  max_contracts: 50
  stc_scaler: true
exclusions:
  filter_stage_blocks: ["TM98_97_98C_2_5MIN_BLEED"]
"""


def test_parse_valid_minimal():
    p = parse_proposal(VALID_MINIMAL)
    assert isinstance(p, ParsedProposal)
    assert p.thesis is None
    assert p.filters.asset == ("BTC",)
    assert p.filters.yes_ask_cents is None
    assert p.sizing.rule == "kelly_capped"
    assert p.sizing.fraction == 0.25
    assert p.sizing.max_contracts == 50
    assert p.sizing.stc_scaler is False
    assert p.exclusions.filter_stage_blocks == ()


def test_parse_valid_full():
    p = parse_proposal(VALID_FULL)
    assert p.thesis == "BTC 90-97c with mid-day STC + normal regime"
    assert p.filters.asset == ("BTC", "ETH")
    assert p.filters.yes_ask_cents == (80, 99)
    assert p.filters.edge_threshold == 0.015
    assert p.filters.stc_seconds == (60, 300)
    assert p.filters.hour_of_day_utc == (13, 14, 15, 16)
    assert p.filters.day_of_week == ("mon", "tue", "wed", "thu", "fri")
    assert p.filters.price_tier == ("90-97", "98-99")
    assert p.filters.regime == ("normal", "high")
    assert p.sizing.stc_scaler is True
    assert p.exclusions.filter_stage_blocks == ("TM98_97_98C_2_5MIN_BLEED",)


def test_parsed_proposal_is_hashable():
    """ParsedProposal must be hashable for Ph1c.2 anti-collusion dedup."""
    p1 = parse_proposal(VALID_MINIMAL)
    p2 = parse_proposal(VALID_MINIMAL)
    assert hash(p1) == hash(p2)
    assert p1 == p2
    assert {p1, p2} == {p1}


def test_path_loading(tmp_path):
    p = tmp_path / "proposal.yaml"
    p.write_text(VALID_MINIMAL)
    parsed = parse_proposal(p)
    assert parsed.filters.asset == ("BTC",)


# ── Reject malformed YAML ──────────────────────────────────────────────


def test_reject_malformed_yaml():
    with pytest.raises(SchemaError, match="YAML parse failure"):
        parse_proposal("this is: not: valid: yaml: nested badly: [")


def test_reject_non_mapping_top_level():
    with pytest.raises(SchemaError, match="top-level mapping"):
        parse_proposal("- a list, not a mapping")


def test_reject_empty_string():
    with pytest.raises(SchemaError, match="top-level mapping"):
        parse_proposal("")


# ── Reject unknown keys ────────────────────────────────────────────────


def test_reject_unknown_top_level_key():
    yml = VALID_MINIMAL + "\nunknown_key: 'x'\n"
    with pytest.raises(SchemaError, match="unknown key"):
        parse_proposal(yml)


def test_reject_unknown_filter_subkey():
    yml = """
filters:
  asset: ["BTC"]
  bogus_filter: "x"
sizing:
  rule: "kelly_capped"
  fraction: 0.25
  max_contracts: 50
  stc_scaler: false
"""
    with pytest.raises(SchemaError, match="filters.*unknown key"):
        parse_proposal(yml)


def test_reject_unknown_sizing_subkey():
    yml = """
filters:
  asset: ["BTC"]
sizing:
  rule: "kelly_capped"
  fraction: 0.25
  max_contracts: 50
  stc_scaler: false
  bogus: 1
"""
    with pytest.raises(SchemaError, match="sizing.*unknown key"):
        parse_proposal(yml)


def test_reject_unknown_exclusions_subkey():
    yml = VALID_MINIMAL + "\nexclusions:\n  bogus: ['x']\n"
    with pytest.raises(SchemaError, match="exclusions.*unknown key"):
        parse_proposal(yml)


# ── Reject missing required keys ───────────────────────────────────────


def test_reject_missing_filters():
    yml = """
sizing:
  rule: "kelly_capped"
  fraction: 0.25
  max_contracts: 50
  stc_scaler: false
"""
    with pytest.raises(SchemaError, match="'filters' missing"):
        parse_proposal(yml)


def test_reject_missing_sizing():
    yml = """
filters:
  asset: ["BTC"]
"""
    with pytest.raises(SchemaError, match="'sizing' missing"):
        parse_proposal(yml)


def test_reject_sizing_missing_subkey():
    yml = """
filters:
  asset: ["BTC"]
sizing:
  rule: "kelly_capped"
  fraction: 0.25
  max_contracts: 50
"""
    with pytest.raises(SchemaError, match="stc_scaler.*missing"):
        parse_proposal(yml)


# ── Reject wrong types ─────────────────────────────────────────────────


def test_reject_asset_as_string():
    yml = """
filters:
  asset: "BTC"
sizing:
  rule: "kelly_capped"
  fraction: 0.25
  max_contracts: 50
  stc_scaler: false
"""
    with pytest.raises(SchemaError, match="filters.asset.*expected list"):
        parse_proposal(yml)


def test_reject_fraction_as_string():
    yml = """
filters:
  asset: ["BTC"]
sizing:
  rule: "kelly_capped"
  fraction: "0.25"
  max_contracts: 50
  stc_scaler: false
"""
    with pytest.raises(SchemaError, match="sizing.fraction.*expected float"):
        parse_proposal(yml)


def test_reject_int_as_bool_for_stc_scaler():
    """`isinstance(True, int)` is True in Python — verify we don't
    accidentally accept 0/1 in the bool slot."""
    yml = """
filters:
  asset: ["BTC"]
sizing:
  rule: "kelly_capped"
  fraction: 0.25
  max_contracts: 50
  stc_scaler: 1
"""
    with pytest.raises(SchemaError, match="stc_scaler.*expected bool"):
        parse_proposal(yml)


def test_reject_bool_as_int_for_max_contracts():
    """Conversely: bool must NOT be coerced to int (since bool is
    int subclass, naive isinstance(x, int) would accept True/False)."""
    yml = """
filters:
  asset: ["BTC"]
sizing:
  rule: "kelly_capped"
  fraction: 0.25
  max_contracts: true
  stc_scaler: false
"""
    with pytest.raises(SchemaError, match="max_contracts.*expected int.*bool"):
        parse_proposal(yml)


def test_reject_float_in_int_slot():
    """yes_ask_cents is int-typed; reject float."""
    yml = """
filters:
  asset: ["BTC"]
  yes_ask_cents: [80.5, 99]
sizing:
  rule: "kelly_capped"
  fraction: 0.25
  max_contracts: 50
  stc_scaler: false
"""
    with pytest.raises(SchemaError, match="yes_ask_cents.*expected int"):
        parse_proposal(yml)


# ── Reject enum out-of-range ───────────────────────────────────────────


def test_reject_unknown_asset():
    yml = """
filters:
  asset: ["DOGE"]
sizing:
  rule: "kelly_capped"
  fraction: 0.25
  max_contracts: 50
  stc_scaler: false
"""
    with pytest.raises(SchemaError, match="not in allowed set"):
        parse_proposal(yml)


def test_reject_unknown_sizing_rule():
    yml = """
filters:
  asset: ["BTC"]
sizing:
  rule: "market"
  fraction: 0.25
  max_contracts: 50
  stc_scaler: false
"""
    with pytest.raises(SchemaError, match="sizing.rule.*not in"):
        parse_proposal(yml)


def test_reject_unknown_day_of_week():
    yml = """
filters:
  asset: ["BTC"]
  day_of_week: ["xxx"]
sizing:
  rule: "kelly_capped"
  fraction: 0.25
  max_contracts: 50
  stc_scaler: false
"""
    with pytest.raises(SchemaError, match="not in allowed set"):
        parse_proposal(yml)


def test_reject_unknown_price_tier():
    yml = """
filters:
  asset: ["BTC"]
  price_tier: ["mid"]
sizing:
  rule: "kelly_capped"
  fraction: 0.25
  max_contracts: 50
  stc_scaler: false
"""
    with pytest.raises(SchemaError, match="not in allowed set"):
        parse_proposal(yml)


# ── Reject numeric out-of-range ────────────────────────────────────────


def test_reject_yes_ask_cents_out_of_range():
    """[1, 99] is the valid range; 0 and 100 are out."""
    yml = """
filters:
  asset: ["BTC"]
  yes_ask_cents: [0, 100]
sizing:
  rule: "kelly_capped"
  fraction: 0.25
  max_contracts: 50
  stc_scaler: false
"""
    with pytest.raises(SchemaError, match=r"yes_ask_cents.*\[1, 99\]"):
        parse_proposal(yml)


def test_reject_hour_of_day_out_of_range():
    yml = """
filters:
  asset: ["BTC"]
  hour_of_day_utc: [-1]
sizing:
  rule: "kelly_capped"
  fraction: 0.25
  max_contracts: 50
  stc_scaler: false
"""
    with pytest.raises(SchemaError, match="out of range"):
        parse_proposal(yml)


def test_reject_hour_of_day_24():
    yml = """
filters:
  asset: ["BTC"]
  hour_of_day_utc: [24]
sizing:
  rule: "kelly_capped"
  fraction: 0.25
  max_contracts: 50
  stc_scaler: false
"""
    with pytest.raises(SchemaError, match="out of range"):
        parse_proposal(yml)


def test_accept_hour_of_day_boundary():
    """0 and 23 are the valid endpoints."""
    yml = """
filters:
  asset: ["BTC"]
  hour_of_day_utc: [0, 23]
sizing:
  rule: "kelly_capped"
  fraction: 0.25
  max_contracts: 50
  stc_scaler: false
"""
    p = parse_proposal(yml)
    assert p.filters.hour_of_day_utc == (0, 23)


def test_reject_negative_edge_threshold():
    yml = """
filters:
  asset: ["BTC"]
  edge_threshold: -0.01
sizing:
  rule: "kelly_capped"
  fraction: 0.25
  max_contracts: 50
  stc_scaler: false
"""
    with pytest.raises(SchemaError, match="edge_threshold.*>= 0"):
        parse_proposal(yml)


def test_reject_fraction_above_one():
    yml = """
filters:
  asset: ["BTC"]
sizing:
  rule: "kelly_capped"
  fraction: 1.5
  max_contracts: 50
  stc_scaler: false
"""
    with pytest.raises(SchemaError, match="fraction.*\\(0, 1\\]"):
        parse_proposal(yml)


def test_reject_fraction_zero():
    yml = """
filters:
  asset: ["BTC"]
sizing:
  rule: "kelly_capped"
  fraction: 0
  max_contracts: 50
  stc_scaler: false
"""
    with pytest.raises(SchemaError, match="fraction.*\\(0, 1\\]"):
        parse_proposal(yml)


def test_reject_max_contracts_zero():
    yml = """
filters:
  asset: ["BTC"]
sizing:
  rule: "kelly_capped"
  fraction: 0.25
  max_contracts: 0
  stc_scaler: false
"""
    with pytest.raises(SchemaError, match="max_contracts.*>= 1"):
        parse_proposal(yml)


def test_reject_stc_seconds_out_of_range():
    yml = """
filters:
  asset: ["BTC"]
  stc_seconds: [-1, 3600]
sizing:
  rule: "kelly_capped"
  fraction: 0.25
  max_contracts: 50
  stc_scaler: false
"""
    with pytest.raises(SchemaError, match=r"stc_seconds.*\[0, 3600\]"):
        parse_proposal(yml)


def test_reject_min_max_pair_wrong_length():
    yml = """
filters:
  asset: ["BTC"]
  yes_ask_cents: [80]
sizing:
  rule: "kelly_capped"
  fraction: 0.25
  max_contracts: 50
  stc_scaler: false
"""
    with pytest.raises(SchemaError, match="2-element"):
        parse_proposal(yml)


# ── PyYAML safety ──────────────────────────────────────────────────────


def test_yaml_safe_load_used_not_unsafe():
    """`yaml.safe_load` rejects `!!python/object/apply` (the unsafe
    constructor). Smoke check that we're not using `yaml.load`."""
    malicious = """!!python/object/apply:os.system [echo pwned]"""
    with pytest.raises(SchemaError):
        parse_proposal(malicious)


# ── R1 regression tests ───────────────────────────────────────────────


def test_r1_finding_1_nan_edge_threshold_rejected():
    """NaN < 0 is False so a naive range guard auto-passes a NaN
    edge_threshold. Replay then filters every row (NaN compares
    always False). _expect_float must reject non-finite values."""
    yml = """
filters:
  asset: ["BTC"]
  edge_threshold: .nan
sizing:
  rule: "kelly_capped"
  fraction: 0.25
  max_contracts: 50
  stc_scaler: false
"""
    with pytest.raises(SchemaError, match="must be finite"):
        parse_proposal(yml)


def test_r1_finding_1_inf_edge_threshold_rejected():
    yml = """
filters:
  asset: ["BTC"]
  edge_threshold: .inf
sizing:
  rule: "kelly_capped"
  fraction: 0.25
  max_contracts: 50
  stc_scaler: false
"""
    with pytest.raises(SchemaError, match="must be finite"):
        parse_proposal(yml)


def test_r1_finding_1_nan_fraction_rejected():
    """sizing.fraction=NaN should also be rejected; the existing
    `not (0 < f <= 1)` happens to catch NaN, but let's pin it via
    the explicit finite-check instead."""
    yml = """
filters:
  asset: ["BTC"]
sizing:
  rule: "kelly_capped"
  fraction: .nan
  max_contracts: 50
  stc_scaler: false
"""
    with pytest.raises(SchemaError, match="must be finite"):
        parse_proposal(yml)


def test_r1_finding_3_path_not_found_raises_schema_error(tmp_path):
    """parse_proposal(Path(missing)) must raise SchemaError, not bare
    FileNotFoundError, so Ph1c.2 fast-reject can route it through
    the same exception channel."""
    missing = tmp_path / "no-such-file.yaml"
    with pytest.raises(SchemaError, match="path .*no-such-file"):
        parse_proposal(missing)


def test_r1_finding_5_duplicate_keys_rejected():
    """PyYAML's default safe_load silently takes last value on dup
    keys. Custom DupKeyDetectingLoader rejects them."""
    yml = """
filters:
  asset: ["BTC"]
  asset: ["ETH"]
sizing:
  rule: "kelly_capped"
  fraction: 0.25
  max_contracts: 50
  stc_scaler: false
"""
    with pytest.raises(SchemaError, match="duplicate key"):
        parse_proposal(yml)


def test_r1_finding_5_duplicate_top_level_keys_rejected():
    yml = """
filters:
  asset: ["BTC"]
sizing:
  rule: "kelly_capped"
  fraction: 0.25
  max_contracts: 50
  stc_scaler: false
sizing:
  rule: "fixed"
  fraction: 0.25
  max_contracts: 50
  stc_scaler: false
"""
    with pytest.raises(SchemaError, match="duplicate key"):
        parse_proposal(yml)


def test_r2_finding_1_byte_cap_uses_utf8_byte_count(tmp_path):
    """MAX_PROPOSAL_BYTES is byte semantics — multi-byte UTF-8 must
    count as multiple bytes. A 600k-CJK-char thesis is 1.8 MB UTF-8;
    must be rejected."""
    big_thesis = "中" * 600_000   # ~1.8 MB UTF-8
    yml = f"""
thesis: "{big_thesis}"
filters:
  asset: ["BTC"]
sizing:
  rule: "kelly_capped"
  fraction: 0.25
  max_contracts: 50
  stc_scaler: false
"""
    with pytest.raises(SchemaError, match="too large"):
        parse_proposal(yml)


def test_r2_finding_2_dedup_str_subset():
    """List fields are semantic sets — duplicates dedup'd at parse."""
    yml = """
filters:
  asset: ["BTC", "BTC", "ETH", "BTC"]
sizing:
  rule: "kelly_capped"
  fraction: 0.25
  max_contracts: 50
  stc_scaler: false
"""
    p = parse_proposal(yml)
    # Dedup preserves first-seen order: BTC then ETH.
    assert p.filters.asset == ("BTC", "ETH")


def test_r2_finding_2_dedup_int_subset():
    yml = """
filters:
  asset: ["BTC"]
  hour_of_day_utc: [13, 13, 14, 13, 15]
sizing:
  rule: "kelly_capped"
  fraction: 0.25
  max_contracts: 50
  stc_scaler: false
"""
    p = parse_proposal(yml)
    assert p.filters.hour_of_day_utc == (13, 14, 15)


def test_r2_finding_2_dedup_regime():
    yml = """
filters:
  asset: ["BTC"]
  regime: ["normal", "normal", "high", "normal"]
sizing:
  rule: "kelly_capped"
  fraction: 0.25
  max_contracts: 50
  stc_scaler: false
"""
    p = parse_proposal(yml)
    assert p.filters.regime == ("normal", "high")


def test_r2_finding_2_dedup_filter_stage_blocks():
    yml = """
filters:
  asset: ["BTC"]
sizing:
  rule: "kelly_capped"
  fraction: 0.25
  max_contracts: 50
  stc_scaler: false
exclusions:
  filter_stage_blocks: ["TM98_97_98C_2_5MIN_BLEED", "TM98_97_98C_2_5MIN_BLEED", "tm96_calmlp_gate_blocked"]
"""
    p = parse_proposal(yml)
    assert p.exclusions.filter_stage_blocks == (
        "TM98_97_98C_2_5MIN_BLEED", "tm96_calmlp_gate_blocked",
    )


def test_r2_finding_5_unicode_decode_error_wrapped(tmp_path):
    """Binary file read produces UnicodeDecodeError; must be wrapped
    as SchemaError so Ph1c.2 fast-reject routes it correctly."""
    bin_path = tmp_path / "binary.yaml"
    bin_path.write_bytes(b"\xff\xfe\x00\x01\x02\x03")
    with pytest.raises(SchemaError, match="path.*binary"):
        parse_proposal(bin_path)


def test_r1_finding_6_oversized_proposal_rejected():
    """1MB+1 byte should be rejected before parsing. Use a long
    thesis to inflate."""
    big_thesis = "x" * (1_000_000 + 100)
    yml = f"""
thesis: "{big_thesis}"
filters:
  asset: ["BTC"]
sizing:
  rule: "kelly_capped"
  fraction: 0.25
  max_contracts: 50
  stc_scaler: false
"""
    with pytest.raises(SchemaError, match="too large"):
        parse_proposal(yml)
