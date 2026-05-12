"""Strategy-drift gate (Bit 3.0.5) — registry-membership invariants.

Replaces the pre-Bit-3.0.5 source-text `count >= 2` heuristic in
`_validate_high_price_stc_block_bleeder_strings()` and
`_validate_bleed_block_bleeder_strings()`. The old heuristic passed for the
wrong reasons on `MAKER_PATIENT`, `terminal_momentum_98`, and `TAKER_NOW`:
their decl-site appearances summed to >= 2 even though no scan-site literal
usage existed (scan sites route through `STRATEGY_*` symbol bindings, and
`terminal_momentum_98` is f-string-built at runtime — never literally
emitted at a scan call site).

Invariant I-3 (the real one):
  Every string in HIGH_PRICE_STC_BLOCK_BLEEDER_STRATEGIES,
  TM98_HIGHPRICE_BLEED_BLOCK_STRATEGIES, and
  SOL_TAKER_LOWPRICE_BLEED_BLOCK_STRATEGIES MUST be a member of the
  live-strategy registry: union of STRATEGY_CLAMP_POLICY keys,
  MAKER_TAIL_ELIGIBLE_STRATEGIES, TM_LIVE_STRATEGIES,
  STRATEGY_LIMIT_BUMP_RESERVE_CENTS keys, the STRATEGY_* string constants,
  and KNOWN_DC_STRATEGIES.

Drift a bleeder out of the registry → silent gate no-op. CI fail-loud
(this file). Boot fail-loud (bot/_impl.py via the boot-time
`_HPSB_MISSING_BLEEDERS = _validate_high_price_stc_block_bleeder_strings()`
binding).

See kb/decisions/bit-3.0.5-validator-decoupling.md.
"""
import inspect
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import bot
import bot.boot  # noqa: F401
import bot.constants  # noqa: F401
import bot.helpers  # noqa: F401
import bot.helpers.validators  # noqa: F401


# Tests below invoke `bot.helpers.validators._validate_bleeders_against_runtime_registry`
# DIRECTLY (the production helper) rather than reimplementing the registry
# composition in test code. R1b M3 lesson: a parallel test-side helper
# would drift from production and provide false confidence.


def test_hpsb_bleeders_subset_of_live_registry():
    """Production validator returns no missing bleeders for the HPSB set."""
    missing = bot.helpers.validators._validate_bleeders_against_runtime_registry(
        bot.constants.HIGH_PRICE_STC_BLOCK_BLEEDER_STRATEGIES, "TEST_HPSB")
    assert missing == [], (
        f"HPSB bleeders unknown to runtime registry: {missing}. "
        f"Strategy may have been renamed; verify STRATEGY_CLAMP_POLICY / "
        f"MAKER_TAIL_ELIGIBLE_STRATEGIES / TM_LIVE_STRATEGIES / "
        f"KNOWN_DC_STRATEGIES / STRATEGY_* constants match the bleeder set, "
        f"or update HIGH_PRICE_STC_BLOCK_BLEEDER_STRATEGIES.")


def test_tm98_bleed_block_bleeders_subset_of_live_registry():
    """Production validator returns no missing bleeders for the TM98 set."""
    missing = bot.helpers.validators._validate_bleeders_against_runtime_registry(
        bot.constants.TM98_HIGHPRICE_BLEED_BLOCK_STRATEGIES, "TEST_TM98")
    assert missing == [], (
        f"TM98 bleed-block bleeders unknown to live registry: {missing}.")


def test_sol_taker_lowprice_bleed_block_bleeders_subset_of_live_registry():
    """Production validator returns no missing bleeders for the SOL_TAKER set."""
    missing = bot.helpers.validators._validate_bleeders_against_runtime_registry(
        bot.constants.SOL_TAKER_LOWPRICE_BLEED_BLOCK_STRATEGIES, "TEST_SOL_TAKER")
    assert missing == [], (
        f"SOL_TAKER bleed-block bleeders unknown to live registry: {missing}.")


def test_boot_validator_returns_empty_at_head():
    """At HEAD, both boot-time validation results MUST be empty. Mirror of
    tests/integration/test_high_price_stc_band_gate.py::test_no_bleeders_missing_at_startup
    against the new validator surface."""
    assert bot.boot._HPSB_MISSING_BLEEDERS == [], bot.boot._HPSB_MISSING_BLEEDERS
    assert bot.boot._BLEED_BLOCK_MISSING_BLEEDERS == [], bot.boot._BLEED_BLOCK_MISSING_BLEEDERS


def test_validator_does_not_read_source_file():
    """Bit 3.0.5 invariant: the new shared validator MUST NOT do
    `open(__file__)` or any source-text grep. Catches reverts to the
    pre-Bit-3.0.5 heuristic. See kb/decisions/bit-3.0.5-validator-decoupling.md."""
    src = inspect.getsource(bot.helpers.validators._validate_bleeders_against_runtime_registry)
    assert "open(" not in src, (
        f"Validator regressed to source-introspection (open() call): {src!r}")
    assert "__file__" not in src, (
        f"Validator regressed to source-introspection (__file__ reference): {src!r}")
    assert ".count(" not in src, (
        f"Validator regressed to source-text count heuristic (.count() call): {src!r}")


def test_validator_uses_runtime_registry_sources():
    """Validator body must reference each registry source by name. Pins the
    dependency surface — if a future refactor renames any registry source,
    this test calls out which symbol no longer reaches the validator."""
    src = inspect.getsource(bot.helpers.validators._validate_bleeders_against_runtime_registry)
    for required in ("STRATEGY_CLAMP_POLICY", "MAKER_TAIL_ELIGIBLE_STRATEGIES",
                     "TM_LIVE_STRATEGIES", "STRATEGY_LIMIT_BUMP_RESERVE_CENTS",
                     "STRATEGY_TAKER_NOW", "STRATEGY_MAKER_PATIENT",
                     "STRATEGY_MAKER_AGGRESSIVE", "STRATEGY_PANIC_CAPTURE",
                     "KNOWN_DC_STRATEGIES"):
        assert required in src, (
            f"Validator must reference {required} as a registry source; "
            f"missing from validator body. See bit-3.0.5-validator-decoupling.md.")


def test_drift_simulation_synthetic_bleeder_caught(caplog):
    """Negative test: a bogus bleeder name MUST be caught by the registry-
    membership invariant. Invokes the PRODUCTION validator directly (not a
    test-side mirror) — guards against the validator returning [] for any
    input, which would silently pass every membership test in this file.

    `caplog.at_level(CRITICAL)` suppresses the validator's expected ERROR log
    line so the test output stays clean — the assert below verifies the
    drift was caught."""
    synthetic = frozenset({"unknown_strategy_xyz_nonexistent"})
    with caplog.at_level(logging.CRITICAL):
        missing = bot.helpers.validators._validate_bleeders_against_runtime_registry(synthetic, "TEST_DRIFT")
    assert missing == ["unknown_strategy_xyz_nonexistent"], (
        f"Production validator failed to catch synthetic drift; got: {missing}. "
        f"This means the registry over-approximates or the validator is broken — "
        f"bleeder-rename detection is OFFLINE.")


def test_known_dc_strategies_includes_decided_t2_z2():
    """Critical: `decided_t2_z2` is in HIGH_PRICE_STC_BLOCK_BLEEDER_STRATEGIES
    but is INTENTIONALLY OMITTED from STRATEGY_LIMIT_BUMP_RESERVE_CENTS
    (T2_Z2 was shadowed Apr 1 2026 — no aggressive reserve grant). It is
    NOT in STRATEGY_CLAMP_POLICY (no decided_t* keys there) and NOT in
    MAKER_TAIL_ELIGIBLE_STRATEGIES either. Without KNOWN_DC_STRATEGIES, the
    registry has a hole that fails the HPSB invariant. This test pins the fix.

    Reference scan-site literal usages of `decided_t2_z2` (post-Bit-3.0.5
    line numbers): bot/_impl.py:~14614 (_dc_strat mapping), ~16477 (tuple
    membership check), ~20494 (tuple membership check). All real production
    code paths."""
    assert "decided_t2_z2" in bot.constants.KNOWN_DC_STRATEGIES, (
        "decided_t2_z2 MUST be in KNOWN_DC_STRATEGIES — see bit-3.0.5-validator-decoupling.md.")


def test_known_dc_strategies_covers_all_dc_tiers():
    """The DC code path recognizes six strategies total: five via the
    `_dc_strat` mapping at bot/_impl.py:~14614 (decided_t1, decided_t1b,
    decided_t2, decided_t2_z25, decided_t2_z2) plus `hourly_dc` set as a
    literal at the hourly DC scan path (bot/_impl.py:~14720, ~14727).
    Pin the full set so a future DC tier addition has to update both sites."""
    expected = {"decided_t1", "decided_t1b", "decided_t2", "decided_t2_z2",
                "decided_t2_z25", "hourly_dc"}
    missing = expected - set(bot.constants.KNOWN_DC_STRATEGIES)
    assert not missing, f"KNOWN_DC_STRATEGIES missing DC tier(s): {sorted(missing)}"


def test_validator_unavailable_reason_is_always_none():
    """Bit 3.0.5: registry-membership validator cannot fail with FileNotFoundError
    (it doesn't read the filesystem). _HPSB_VALIDATOR_UNAVAILABLE_REASON is
    therefore vestigial — kept as None for the HPSB_GATE_STATE log consumer
    at bot/_impl.py:~26243 ("validator_unavailable=no" output)."""
    assert bot.boot._HPSB_VALIDATOR_UNAVAILABLE_REASON is None, (
        f"Vestigial global must remain None post-Bit-3.0.5; got: "
        f"{bot.boot._HPSB_VALIDATOR_UNAVAILABLE_REASON!r}")
