"""D-24 — counterfactual_pnl rounding (integer cents only).

Authoritative source: evaluated_opportunities.counterfactual_pnl column type is
INTEGER (per RCA D-24 + the DDL at bot.py:3232). The would_have_profit value is
computed as integer-cent arithmetic — no fractional cents possible.

Replay's cf must use Python int throughout. numpy.int64 / pandas.Int64 are not
acceptable in the return value because they don't round-trip cleanly through
SQL WHERE clauses (a numpy.float64(123.0) compares-equal to 123 in Python but
NOT in SQL).

This is also a generalized D-2 mutation defense: anything that lets a float
leak into the cf path will surface here.
"""
from __future__ import annotations

import pytest

from research.replay import replay_cf_pnl, replay_maker_fee, replay_taker_fee


# Cases that exercise win/loss/None branches.
INT_RETURN_CASES = [
    # (kwargs, expected_int_or_None) — values computed via the canonical fee formula
    # ceil(0.07 * count * price * (100-price) / 100). Kalshi's 7% rate × (1-price)
    # gives 1c fees at extreme prices; 2c fees near the 50c midpoint.
    (dict(entry_price=85, market_result="yes", side="yes", position_size=1, product_type="15m"), 14),     # (100-85)*1 - fee(1,85)=1 -> 14
    (dict(entry_price=85, market_result="no",  side="yes", position_size=1, product_type="15m"), -86),    # -(85*1 + 1) -> -86
    (dict(entry_price=50, market_result="yes", side="yes", position_size=10, product_type="15m"), 482),   # (100-50)*10 - fee(10,50)=18 -> 482
    (dict(entry_price=8,  market_result="yes", side="yes", position_size=10, product_type="weather"), 0), # weather below floor=10 -> 0
    (dict(entry_price=None, market_result="yes", side="yes", position_size=1, product_type="15m"), None),
    (dict(entry_price=85, market_result="all_neutral", side="yes", position_size=1, product_type="15m"), None),
]


@pytest.mark.parametrize("kwargs,expected", INT_RETURN_CASES)
def test_d24_replay_cf_pnl_returns_pure_int_or_none(kwargs: dict, expected: object) -> None:
    """Replay cf returns pure-int or None — never float, never numpy/pandas types.

    Use `type(x) is int` (NOT isinstance) because:
    - bool is a subclass of int in Python (`isinstance(True, int)` is True),
      and we don't want True/False slipping through.
    - numpy.int64 is NOT a Python int (passes isinstance via __index__,
      depending on numpy version), but `type(np.int64(5)) is int` is False.
    """
    cf = replay_cf_pnl(**kwargs)
    if expected is None:
        assert cf is None, f"D-24 expected None, got {cf!r} ({type(cf).__name__})"
    else:
        assert type(cf) is int, (
            f"D-24 cf type drift: {kwargs} -> {cf!r} ({type(cf).__name__}), "
            f"expected pure int"
        )
        assert cf == expected, f"D-24 value: expected {expected}, got {cf}"


def test_d24_replay_taker_fee_returns_pure_int() -> None:
    """Fee returns pure-int across the full price range."""
    for count in (1, 10, 100):
        for price in range(1, 100):
            fee = replay_taker_fee(count, price)
            assert type(fee) is int, (
                f"D-24 fee type drift: count={count} price={price} -> "
                f"{fee!r} ({type(fee).__name__})"
            )


def test_d24_replay_maker_fee_returns_pure_int() -> None:
    """Maker fee is 0 and must be int (not float-zero)."""
    fee = replay_maker_fee(1, 50)
    assert type(fee) is int, f"D-24 maker-fee type: {fee!r} ({type(fee).__name__})"
    assert fee == 0


def test_d24_no_fractional_cents_in_win_case() -> None:
    """Sweep across (count × price) and confirm no fractional remainder leaks in.

    If anyone touches the cf formula to use `/` instead of `//` (float division)
    or removes the `math.ceil` wrap on the fee, this test surfaces the drift.
    """
    for count in (1, 5, 17):  # primes + small
        for price in (33, 67, 89):  # awkward primes
            cf_win = replay_cf_pnl(
                entry_price=price,
                market_result="yes",
                side="yes",
                position_size=count,
                product_type="15m",
            )
            cf_loss = replay_cf_pnl(
                entry_price=price,
                market_result="no",
                side="yes",
                position_size=count,
                product_type="15m",
            )
            assert type(cf_win) is int, (
                f"D-24 win-case float leak: count={count} price={price} -> "
                f"{cf_win!r} ({type(cf_win).__name__})"
            )
            assert type(cf_loss) is int, (
                f"D-24 loss-case float leak: count={count} price={price} -> "
                f"{cf_loss!r} ({type(cf_loss).__name__})"
            )


def test_d24_bool_does_not_slip_through() -> None:
    """`type(x) is int` rejects True/False even though they're int subclasses.

    Defends against a refactor that returns `is_win` directly from the cf
    function. `bool` IS NOT `int` under `type() is int`, even though
    `isinstance(True, int)` is True. Pin the strict type check.
    """
    cf = replay_cf_pnl(
        entry_price=85,
        market_result="yes",
        side="yes",
        position_size=1,
        product_type="15m",
    )
    assert type(cf) is int, f"D-24 strict type: got {type(cf).__name__}"
    assert not isinstance(cf, bool), (
        f"D-24 bool leak: replay returned a bool ({cf!r}), not a true int"
    )
