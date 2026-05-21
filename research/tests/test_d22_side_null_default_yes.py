"""D-22 — side IS NULL legacy rows default to 'yes' (not NULL).

Authoritative source: bot._impl::MainLoop::_poll_evaluated_opportunities cf branch
(per RCA D-22). Live behavior: `side = row.get("side") or "yes"`. The
insert_evaluated_opportunity signature defaults side='yes' too. So pre-side-column
rows (before the column was added) have stored side=NULL and their cf was computed
at the time using the YES-side fallback. Replay must NOT post-hoc reinterpret as
NO-side — those stored cf values reflect YES semantics.

This is one of the "hidden default" bug classes: the fallback is correct for
legacy data, but if a future caller passes side=None expecting "no inference,"
the test catches that contract drift.
"""
from __future__ import annotations

import pytest

from research.replay import replay_cf_pnl


def test_d22_side_null_treated_as_yes_win() -> None:
    """side=None, result='yes' -> WIN (because NULL defaults to YES)."""
    cf = replay_cf_pnl(
        entry_price=85,
        market_result="yes",
        side=None,
        position_size=1,
        product_type="15m",
    )
    # YES side WIN @ 85: (100-85)*1 - fee(1,85) = 15 - 1 = 14
    assert cf == 14, f"D-22 NULL→YES win: expected 14, got {cf!r}"


def test_d22_side_null_treated_as_yes_loss() -> None:
    """side=None, result='no' -> LOSS (because NULL defaults to YES, and 'no' is opposite)."""
    cf = replay_cf_pnl(
        entry_price=85,
        market_result="no",
        side=None,
        position_size=1,
        product_type="15m",
    )
    # YES side LOSS @ 85: -(85*1 + 1) = -86
    assert cf == -86, f"D-22 NULL→YES loss: expected -86, got {cf!r}"


def test_d22_explicit_no_side_inverts_predicate() -> None:
    """side='no', result='no' -> WIN (NO-side win condition)."""
    cf = replay_cf_pnl(
        entry_price=85,
        market_result="no",
        side="no",
        position_size=1,
        product_type="15m",
    )
    # NO side @ 85, result=no -> WIN: (100-85)*1 - fee(1,85) = 14
    assert cf == 14, f"D-22 NO-side win: expected 14, got {cf!r}"


def test_d22_explicit_no_side_yes_result_is_loss() -> None:
    """side='no', result='yes' -> LOSS (NO-side loss condition)."""
    cf = replay_cf_pnl(
        entry_price=85,
        market_result="yes",
        side="no",
        position_size=1,
        product_type="15m",
    )
    # NO side @ 85, result=yes -> LOSS: -(85+1) = -86
    assert cf == -86, f"D-22 NO-side loss: expected -86, got {cf!r}"


@pytest.mark.parametrize("side_in", [None, "yes", "YES", "Yes"])
def test_d22_yes_normalization(side_in: str) -> None:
    """side ∈ {NULL, 'yes', 'YES', 'Yes'} all map to YES.

    Per `side = (side or "yes").lower()` — case normalization matches the
    `lower()` in the result branch too.
    """
    cf = replay_cf_pnl(
        entry_price=85,
        market_result="yes",
        side=side_in,
        position_size=1,
        product_type="15m",
    )
    assert cf == 14, f"D-22 YES-side normalization: side_in={side_in!r} expected 14, got {cf!r}"


@pytest.mark.parametrize("side_in", ["no", "NO", "No"])
def test_d22_no_normalization(side_in: str) -> None:
    """Case normalization applies to NO-side too."""
    cf = replay_cf_pnl(
        entry_price=85,
        market_result="no",
        side=side_in,
        position_size=1,
        product_type="15m",
    )
    # NO side win @ 85, result=no: (100-85)*1 - 1 = 14
    assert cf == 14, f"D-22 NO-side normalization: side_in={side_in!r} expected 14, got {cf!r}"


def test_d22_empty_string_side_is_yes() -> None:
    """side='' is falsy in Python -> defaults to 'yes' via `or`."""
    cf = replay_cf_pnl(
        entry_price=85,
        market_result="yes",
        side="",
        position_size=1,
        product_type="15m",
    )
    assert cf == 14, f"D-22 empty-string side: expected 14 (YES default), got {cf!r}"
