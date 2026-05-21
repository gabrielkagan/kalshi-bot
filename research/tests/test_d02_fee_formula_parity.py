"""D-2 — fee formula off-by-100 regression test.

Authoritative source: models.calculate_fee (taker) — formula returns CENTS, not dollars.

    ceil(fee_mult_taker × count × price_cents × (100 − price_cents) / 100)

The bug class this guards against (per kb/decisions/replay-engine-rca-2026-05-05.md
section D-2): scripts/alpha_audit.py at one point defined a local taker_fee_cents
that used `ceil(0.07 × contracts × p × (1-p))` where `p = price_cents/100.0` —
that evaluated the dollar-form and ceiled to integer DOLLARS, returning a number
100× too small at sub-90c prices.

Concrete canonical values (RCA example: 1 ct @ 75c → cents = 2):
    ceil(0.07 × 1 × 75 × 25 / 100) = ceil(1.3125) = 2

If the divisor drifts from 100 (e.g., to 10000), or if the formula ceils dollars
instead of cents, multiple of these assertions fail.

Replay constant pinned: replay_taker_fee defaults fee_mult_taker=0.07 (matches the
default fee_mult_taker on `models.calculate_fee` for non-SPX product types). See
D-16 for the SPX 0.035 variant (intentionally NOT applied at the live cf path).
"""
from __future__ import annotations

import math

import pytest

from research.replay import replay_maker_fee, replay_taker_fee


# Canonical (count, price_cents, expected_cents) tuples computed from the
# bytewise formula. Note: Kalshi's 7% fee × price × (1-price) gives surprisingly
# small fees at extreme prices because (1-price) shrinks toward 0 — fee at 85c
# is essentially 1c per contract. Don't intuit; derive.
CANONICAL_FEES = [
    # (count, price, expected)
    (1, 75, 2),     # ceil(0.07 * 1 * 75 * 25 / 100) = ceil(1.3125) = 2
    (1, 85, 1),     # ceil(0.07 * 1 * 85 * 15 / 100) = ceil(0.8925) = 1
    (1, 90, 1),     # ceil(0.07 * 1 * 90 * 10 / 100) = ceil(0.63)   = 1
    (1, 96, 1),     # ceil(0.07 * 1 * 96 *  4 / 100) = ceil(0.2688) = 1
    (1, 50, 2),     # ceil(0.07 * 1 * 50 * 50 / 100) = ceil(1.75)   = 2
    (10, 85, 9),    # 10× scale: ceil(0.07 * 10 * 85 * 15 / 100) = ceil(8.925) = 9
    (10, 50, 18),   # ceil(0.07 * 10 * 50 * 50 / 100) = ceil(17.5)  = 18
    (1, 1, 1),      # edge low: ceil(0.07 * 1 * 1 * 99 / 100) = ceil(0.0693) = 1
    (1, 99, 1),     # edge high: ceil(0.07 * 1 * 99 * 1 / 100) = ceil(0.0693) = 1
]


@pytest.mark.parametrize("count,price_cents,expected_cents", CANONICAL_FEES)
def test_d02_replay_taker_fee_canonical(count: int, price_cents: int, expected_cents: int) -> None:
    """Replay taker fee matches the hand-computed cents byte-for-byte."""
    actual = replay_taker_fee(count, price_cents)
    assert actual == expected_cents, (
        f"D-2 divergence: count={count} price={price_cents}c -> "
        f"replay={actual} expected={expected_cents}"
    )


@pytest.mark.parametrize("count,price_cents,_expected", CANONICAL_FEES)
def test_d02_replay_taker_fee_returns_int(count: int, price_cents: int, _expected: int) -> None:
    """Pure-int return (D-24 reinforcement). Catches numpy.int64 / float drift."""
    actual = replay_taker_fee(count, price_cents)
    assert type(actual) is int, (
        f"D-2/D-24 divergence: replay_taker_fee({count}, {price_cents}) returned "
        f"{type(actual).__name__}={actual!r}, expected pure-int"
    )


def test_d02_replay_taker_fee_formula_pinned() -> None:
    """Pin the exact formula. Catches any divisor/exponent drift in a single test.

    If anyone changes the formula in research/replay.py:replay_taker_fee, this
    test fails clearly with the divergence visible in the assertion message.
    """
    for count in (1, 5, 10):
        for price in range(1, 100):
            expected = math.ceil(0.07 * count * price * (100 - price) / 100)
            actual = replay_taker_fee(count, price)
            assert actual == expected, (
                f"D-2 formula drift: count={count} price={price}c -> "
                f"replay={actual} canonical={expected}"
            )


def test_d02_replay_taker_fee_spx_discount() -> None:
    """SPX uses fee_mult_taker=0.035 (50% finance-category discount).

    Authoritative constant: bot.constants.SPX_HOURLY_FEE_MULTIPLIER_TAKER.

    NOTE: the live _poll_evaluated_opportunities path uses the default 0.07 for
    ALL product_types (RCA D-16). So the replay_cf_pnl byte-equality gate (D-1)
    uses 0.07 even for SPX rows. This test pins ONLY the calculator's parameter
    plumbing — D-16 has the xfail documenting that live cf is likely under-fee'd
    on SPX rows.
    """
    # 1 ct @ 50c with SPX rate: ceil(0.035 * 1 * 50 * 50 / 100) = ceil(0.875) = 1
    assert replay_taker_fee(1, 50, fee_mult_taker=0.035) == 1
    # vs crypto at the same price/count: ceil(0.07 * 1 * 50 * 50 / 100) = ceil(1.75) = 2
    crypto = replay_taker_fee(1, 50, fee_mult_taker=0.07)   # = 2
    spx = replay_taker_fee(1, 50, fee_mult_taker=0.035)     # = 1
    # Pin both values directly. Ratio comparisons are noisy because of integer
    # ceiling (e.g. at 75c crypto=2 vs spx=1, ratio 2:1; at other prices ceiling
    # truncates differently). The literal values are the contract.
    assert (crypto, spx) == (2, 1), f"SPX/crypto fee drift: crypto={crypto} spx={spx}"


def test_d02_replay_maker_fee_zero() -> None:
    """Maker fee is $0 across all product types (RCA D-2 + canonical models.py).

    AST-level pin: any modification of replay_maker_fee to return non-zero gets
    caught here. Catches the bug class where a paste-mistake mixed up maker/taker.
    """
    for count in (1, 10, 100, 1000):
        for price in (1, 50, 85, 99):
            assert replay_maker_fee(count, price) == 0, (
                f"D-2 maker-fee divergence: count={count} price={price}c -> "
                f"replay={replay_maker_fee(count, price)} expected=0"
            )
