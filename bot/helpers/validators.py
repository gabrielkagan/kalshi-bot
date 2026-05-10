"""Bit 3.2: Bleeder-string runtime-registry validators, extracted from bot/_impl.py.

Per Bit 3.0.5: validators delegate to runtime-registry membership (no source-grep). The boot-time invocations
`_HPSB_MISSING_BLEEDERS = _validate_high_price_stc_block_bleeder_strings()` and
`_BLEED_BLOCK_MISSING_BLEEDERS = _validate_bleed_block_bleeder_strings()`
remain in bot/_impl.py so the runtime-state bindings stay at the legacy location.
"""
import logging
from typing import List

from bot.constants import *  # noqa: F401,F403 — strategy registries + bleeder lists

def _validate_bleeders_against_runtime_registry(bleeder_set, name):
    """Boot-time check: every string in `bleeder_set` MUST be a member of the
    live-strategy registry — i.e. a name that some bleeder-relevant subsystem
    treats as a real `candidate.strategy`. Drift = ERROR log + non-empty
    return; gate would silently no-op without this check (a bleeder that
    doesn't match any candidate.strategy is never blocked).

    Registry sources (NOT the full set of strategies in the codebase — names
    like `weather_no_live`, `hourly_no_live`, `hourly_dc_*` exist at scan
    sites but are intentionally outside the bleeder-gate scope):
      - STRATEGY_CLAMP_POLICY keys (sub-limit clamp policy)
      - MAKER_TAIL_ELIGIBLE_STRATEGIES (post-IOC partial maker-tail)
      - TM_LIVE_STRATEGIES (frozenset(f"terminal_momentum_{p}" for p in TM_PRICE_SET))
      - STRATEGY_LIMIT_BUMP_RESERVE_CENTS keys (smart IOC limit picker)
      - STRATEGY_TAKER_NOW / STRATEGY_MAKER_PATIENT / STRATEGY_MAKER_AGGRESSIVE
        / STRATEGY_PANIC_CAPTURE — canonical execution-engine names
        (technically redundant with STRATEGY_CLAMP_POLICY but explicit > implicit).
        STRATEGY_WAIT is INTENTIONALLY EXCLUDED — it's a no-op signal returned
        by evaluate_execution_strategy(), never set as candidate.strategy.
        Including it would let a hypothetical rename `MAKER_PATIENT → WAIT`
        silently pass the validator.
      - KNOWN_DC_STRATEGIES (decided-contract names — see comment above)
    """
    live = (set(STRATEGY_CLAMP_POLICY.keys())
            | set(MAKER_TAIL_ELIGIBLE_STRATEGIES)
            | set(TM_LIVE_STRATEGIES)
            | set(STRATEGY_LIMIT_BUMP_RESERVE_CENTS.keys())
            | {STRATEGY_TAKER_NOW, STRATEGY_MAKER_PATIENT,
               STRATEGY_MAKER_AGGRESSIVE, STRATEGY_PANIC_CAPTURE}
            | KNOWN_DC_STRATEGIES)
    missing = sorted(bleeder_set - live)
    if missing:
        logging.error(
            "%s_BLEEDER_UNKNOWN_TO_REGISTRY: %s — gate will silently no-op for "
            "these. A strategy may have been renamed; update STRATEGY_CLAMP_POLICY, "
            "MAKER_TAIL_ELIGIBLE_STRATEGIES, TM_LIVE_STRATEGIES, "
            "STRATEGY_LIMIT_BUMP_RESERVE_CENTS, KNOWN_DC_STRATEGIES, or one of "
            "the STRATEGY_* string constants to match the new name.",
            name, missing)
    return missing


def _validate_high_price_stc_block_bleeder_strings():
    """HPSB bleeder integrity check (Bit 3.0.5: registry-membership)."""
    return _validate_bleeders_against_runtime_registry(
        HIGH_PRICE_STC_BLOCK_BLEEDER_STRATEGIES, "HPSB")


def _validate_bleed_block_bleeder_strings():
    """TM98 + SOL_TAKER + SOL_BLEED_V2 bleed-block bleeder integrity check (Bit 3.0.5: registry-membership)."""
    return _validate_bleeders_against_runtime_registry(
        TM98_HIGHPRICE_BLEED_BLOCK_STRATEGIES
        | SOL_TAKER_LOWPRICE_BLEED_BLOCK_STRATEGIES
        | SOL_BLEED_V2_BLOCK_STRATEGIES,
        "BLEED_BLOCK")
