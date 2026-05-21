"""D-16 — per-product fee schedule + the SPX cf under-fee bug class.

Authoritative source: bot._impl::MainLoop::_poll_evaluated_opportunities cf
computation (per RCA D-16).

Two contracts:

1. **Replay cf matches live cf** — the live cf path calls calculate_taker_fee
   with NO product-aware branching. So replay_cf_pnl must use fee_mult_taker=0.07
   for ALL product_types (including 'spx_hourly'), even though SPX trades at
   0.035 in production (per `bot.constants.SPX_HOURLY_FEE_MULTIPLIER_TAKER`).

2. **Document the live-cf SPX bug** — there's a latent under-fee bug at the
   live cf path: SPX cf overstates fees by 2× (uses 0.07 instead of 0.035) →
   understates cf for SPX. Replay matches the live (buggy) behavior because
   D-1 byte-equality is the gate; replay does NOT "fix" it. The xfail test
   below pins the bug for Phase 5 ship-doc surfacing.

DO NOT replace the xfail with a fix — that would break the D-1 cf-identity gate.
"""
from __future__ import annotations

import pytest

from research.replay import replay_cf_pnl, replay_taker_fee


@pytest.mark.parametrize("product_type", [
    "15m", "hourly", "spx_hourly", "weather", "sports", None, "futureproduct",
])
def test_d16_replay_cf_uses_default_fee_for_all_products(product_type: str) -> None:
    """replay_cf_pnl uses fee_mult_taker=0.07 default regardless of product_type.

    The live cf path has NO product-aware fee branching (RCA D-16). Replay
    matches that. If replay starts branching on product_type to pick a different
    fee rate, the D-1 byte-equality gate on the snapshot breaks.
    """
    # WIN @ 85, 1ct, with default fee should give cf = (100-85) - fee_at_0.07 = 14
    # For weather rows at entry=85, weather_min_entry=10 doesn't gate (entry > floor).
    cf = replay_cf_pnl(
        entry_price=85,
        market_result="yes",
        side="yes",
        position_size=1,
        product_type=product_type,
    )
    assert cf == 14, (
        f"D-16 product-aware drift: product={product_type!r} -> {cf!r}, "
        f"expected 14 (canonical default-fee cf)"
    )


def test_d16_replay_taker_fee_default_is_crypto_rate() -> None:
    """Default fee_mult_taker is 0.07 (crypto rate), per bot.constants default in models.calculate_fee.

    Pins the default. If someone changes the default to 0.035 in
    research/replay.py:replay_taker_fee signature, this test fails clearly.
    """
    # At 50c, 1ct: 0.07 -> ceil(1.75) = 2; 0.035 -> ceil(0.875) = 1.
    fee_default = replay_taker_fee(1, 50)               # implicit default
    fee_explicit_crypto = replay_taker_fee(1, 50, fee_mult_taker=0.07)
    assert fee_default == fee_explicit_crypto == 2, (
        f"D-16 default-rate drift: default={fee_default} explicit_crypto={fee_explicit_crypto}"
    )


@pytest.mark.xfail(
    reason="Live cf path uses fee_mult_taker=0.07 for SPX too — under-fee'd by ~2x. "
           "Replay matches this bug per D-1 byte-equality gate. Surface in B2 ship-doc.",
    strict=True,  # if this ever PASSES, the live-cf bug is fixed and we need to update D-1 + D-16
)
def test_d16_spx_cf_should_use_discount_rate_xfail() -> None:
    """If live cf ever starts using 0.035 for SPX, this xfail flips to PASS.

    That would be a sign that the live-cf-fee-mismatch was fixed in bot/_impl
    (or its post-9.3-iii.c canonical home). When it flips, we must:
    1. Update replay_cf_pnl to accept fee_mult_taker per-row OR per product.
    2. Update D-1 byte-equality expected values for SPX snapshot rows.
    3. Convert this xfail into a regular passing test.

    strict=True so a silent fix doesn't go unnoticed.
    """
    # Hypothetical "fixed" SPX cf at 1ct @ 50c WIN:
    #   crypto: (100-50)*1 - ceil(0.07*50*50/100) = 50 - 2 = 48
    #   SPX:    (100-50)*1 - ceil(0.035*50*50/100) = 50 - 1 = 49
    # Replay returns 48 (matches live). The "fixed" expectation would be 49.
    cf = replay_cf_pnl(
        entry_price=50,
        market_result="yes",
        side="yes",
        position_size=1,
        product_type="spx_hourly",
    )
    # If live were fixed and replay tracked it, cf would be 49.
    # Today, cf is 48 (the under-fee'd live value). XFAIL.
    assert cf == 49, (
        f"D-16 SPX under-fee bug: replay returned {cf} matching live (0.07 rate). "
        f"If this assertion ever holds, the live-cf bug was fixed and D-1 needs an update."
    )


def test_d16_no_product_specific_fee_constants_imported() -> None:
    """research/replay.py does NOT import product-specific fee constants.

    Pins the contract that replay is a clean port operating on stable defaults.
    If someone refactors to import bot.constants.SPX_HOURLY_FEE_MULTIPLIER_TAKER,
    the D-1 byte-equality gate likely breaks because replay starts branching.
    Catches early.
    """
    import research.replay as rep
    import inspect
    source = inspect.getsource(rep)
    # Lightweight AST-substring guard. If you genuinely need to import these
    # for a future D-NN, add an explicit allowlist comment + update this test.
    forbidden_substrings = [
        "SPX_HOURLY_FEE_MULTIPLIER",
        "from bot.constants",
        "from bot import constants",
        "import bot.constants",
    ]
    for needle in forbidden_substrings:
        assert needle not in source, (
            f"D-16 forbidden import in research/replay.py: {needle!r}. "
            f"Replay is parallel-track — clean port, no bot.* imports."
        )
