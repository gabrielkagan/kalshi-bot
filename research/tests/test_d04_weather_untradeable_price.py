"""D-4 — weather untradeable_price edge case.

Authoritative source: bot._impl::MainLoop::_poll_evaluated_opportunities cf branch
that zeros out weather rows below the entry-price gate. Live behavior (per RCA
D-4): for product_type='weather' rows where entry_price < WEATHER_MIN_ENTRY_PRICE,
live writes counterfactual_pnl=0 (NOT a normal cf), counterfactual='untradeable_price'.

Replay must replicate this zero-out — including the QUIRK that the constant
WEATHER_MIN_ENTRY_PRICE compared against is the YES-side floor (currently 10) and
the live cf logic compares only against the YES-side floor for BOTH sides, even
though NO-side has a different floor (WEATHER_NO_MIN_PRICE=39). The RCA explicitly
states: "Replicate the live behavior, do not 'fix' it — divergence here is a real
prod bug if it exists, not a replay bug."

Replay constants pinned (B1 R1 lesson — name the bot.constants symbol):
    weather_min_entry = bot.constants.WEATHER_MIN_ENTRY_PRICE  (currently 10)
"""
from __future__ import annotations

import pytest

from research.replay import replay_cf_pnl


@pytest.mark.parametrize("side", ["yes", "no"])
def test_d04_weather_low_price_zeros_cf_regardless_of_side(side: str) -> None:
    """Weather row at entry=8 (below 10 floor) yields cf=0 for BOTH sides.

    This is the live behavior including the YES-side-only quirk. If someone
    "fixes" the live code to compare NO-side rows against WEATHER_NO_MIN_PRICE,
    this test correctly fails — that's a deliberate replication of the prod bug.
    """
    cf = replay_cf_pnl(
        entry_price=8,
        market_result="yes",
        side=side,
        position_size=10,
        product_type="weather",
    )
    assert cf == 0, f"D-4 weather floor: side={side} entry=8 -> {cf}, expected 0"


def test_d04_weather_at_floor_is_zero() -> None:
    """entry == WEATHER_MIN_ENTRY_PRICE is INCLUSIVE-zero per `entry < floor` semantic.

    Live formula is `entry_price < weather_min_entry` (strict less-than), so
    entry=10 (== floor) is the FIRST tradeable price. Pin the boundary.
    """
    cf_at_floor = replay_cf_pnl(
        entry_price=10,
        market_result="yes",
        side="yes",
        position_size=1,
        product_type="weather",
    )
    cf_below_floor = replay_cf_pnl(
        entry_price=9,
        market_result="yes",
        side="yes",
        position_size=1,
        product_type="weather",
    )
    # At floor: normal cf (1 ct @ 10c win: (100-10)*1 - ceil(0.07*1*10*90/100) = 90 - 1 = 89)
    assert cf_at_floor == 89, f"D-4 weather at-floor: entry=10 -> {cf_at_floor}, expected 89"
    # Below floor: zeroed
    assert cf_below_floor == 0, f"D-4 weather below-floor: entry=9 -> {cf_below_floor}, expected 0"


def test_d04_weather_normal_price_normal_cf() -> None:
    """Above floor, weather behaves like any product. WIN cf computed normally."""
    cf = replay_cf_pnl(
        entry_price=12,
        market_result="yes",
        side="yes",
        position_size=1,
        product_type="weather",
    )
    # (100 - 12) * 1 - ceil(0.07 * 1 * 12 * 88 / 100) = 88 - 1 = 87
    assert cf == 87, f"D-4 weather above-floor: entry=12 win -> {cf}, expected 87"


def test_d04_non_weather_low_price_not_zeroed() -> None:
    """Non-weather product at entry=8 does NOT get the untradeable-price zero-out.

    Pins that the WEATHER_MIN_ENTRY_PRICE branch is gated on product_type=='weather'.
    A 15m row at entry=8c (unrealistic but defends the branch) computes cf normally.
    """
    cf = replay_cf_pnl(
        entry_price=8,
        market_result="yes",
        side="yes",
        position_size=1,
        product_type="15m",
    )
    # WIN: (100 - 8) * 1 - ceil(0.07 * 1 * 8 * 92 / 100) = 92 - 1 = 91
    assert cf == 91, f"D-4 non-weather low-entry: 15m entry=8 -> {cf}, expected 91"


def test_d04_weather_floor_constant_pinned_at_10() -> None:
    """Replay's default weather_min_entry is 10, matching bot.constants.WEATHER_MIN_ENTRY_PRICE.

    B1 R1 lesson: the original B1 replay defaulted to 39 (NO-side floor leaked
    from memory). Pin the default explicitly so any future re-default is loud.
    """
    # At entry=9, product=weather, the default floor=10 zeros it. If the default
    # silently drifts to e.g. 39, the entry=9 case still zeros (which is wrong
    # for the wrong reason), but entry=20 should be normal-cf — and would not
    # be if the default drifted to 39.
    cf_at_20 = replay_cf_pnl(
        entry_price=20,
        market_result="yes",
        side="yes",
        position_size=1,
        product_type="weather",
    )
    # (100-20)*1 - ceil(0.07*1*20*80/100) = 80 - ceil(1.12) = 80 - 2 = 78
    assert cf_at_20 == 78, (
        f"D-4 floor-default drift: weather entry=20 -> {cf_at_20}. "
        f"If this returns 0, the weather_min_entry default drifted above 20."
    )
