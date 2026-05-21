"""D-7 — position_size NULL defaults to 1 contract (NOT zero).

Authoritative source: bot._impl::MainLoop::_poll_evaluated_opportunities cf branch
(per RCA D-7). Live formula: `count = row.get("position_size") or 1`. Rows with
NULL position_size (including ALL rejection rows on evaluated_opportunities per
CLAUDE.md "Sim PnL uses actual Kelly sizing — never flat 1-contract") receive a
1-contract cf.

This is the HYBRID behavior at the live cf path: sized rows use their position_size,
NULL rows use 1ct. Replay's `replay_cf_pnl` (D-1 surface) must reproduce this
hybrid identically.

Note: D-13 covers the separate Kelly-mandatory rule for the modified-sizing
sweep path; THAT path raises on NULL inputs rather than defaulting to 1ct.
D-7 is the LIVE-replicated path only.
"""
from __future__ import annotations

import pytest

from research.replay import replay_cf_pnl, replay_taker_fee


def test_d07_null_position_size_defaults_to_1ct_win() -> None:
    """position_size=None on a winning row -> 1ct cf."""
    cf = replay_cf_pnl(
        entry_price=85,
        market_result="yes",
        side="yes",
        position_size=None,
        product_type="15m",
    )
    # 1 ct win @ 85: (100-85)*1 - ceil(0.07*1*85*15/100) = 15 - 1 = 14
    expected = (100 - 85) * 1 - replay_taker_fee(1, 85)
    assert cf == expected == 14, f"D-7 null-size win: expected {expected}, got {cf!r}"


def test_d07_null_position_size_defaults_to_1ct_loss() -> None:
    """position_size=None on a losing row -> 1ct cf (LOSS sign)."""
    cf = replay_cf_pnl(
        entry_price=85,
        market_result="no",
        side="yes",
        position_size=None,
        product_type="15m",
    )
    # 1 ct loss @ 85: -(85*1 + ceil(0.07*1*85*15/100)) = -(85 + 1) = -86
    expected = -(85 * 1 + replay_taker_fee(1, 85))
    assert cf == expected == -86, f"D-7 null-size loss: expected {expected}, got {cf!r}"


def test_d07_zero_position_size_also_defaults_to_1ct() -> None:
    """position_size=0 is FALSY in Python, so `0 or 1 == 1`. Pin this.

    Production data should never produce position_size=0 on a settled row,
    but the `or` short-circuit means we treat it identically to NULL.
    """
    cf = replay_cf_pnl(
        entry_price=85,
        market_result="yes",
        side="yes",
        position_size=0,
        product_type="15m",
    )
    assert cf == 14, f"D-7 zero-size falsy: expected 14 (1ct cf), got {cf!r}"


@pytest.mark.parametrize("size", [1, 5, 10, 100])
def test_d07_real_position_size_uses_that_count(size: int) -> None:
    """Non-NULL position_size scales the cf linearly per the formula."""
    cf = replay_cf_pnl(
        entry_price=85,
        market_result="yes",
        side="yes",
        position_size=size,
        product_type="15m",
    )
    # Win @ 85, size ct: (100-85)*size - ceil(0.07*size*85*15/100)
    expected = (100 - 85) * size - replay_taker_fee(size, 85)
    assert cf == expected, (
        f"D-7 sized cf: size={size} expected={expected} got={cf!r}"
    )


def test_d07_null_size_is_not_skip() -> None:
    """RCA explicit: 'still receive a 1-contract cf' — NOT skipped, NOT NULL.

    The 1ct_sim hybrid is a SEPARATE behavior from D-5 (NULL entry skips).
    Pin that distinction.
    """
    cf = replay_cf_pnl(
        entry_price=85,
        market_result="yes",
        side="yes",
        position_size=None,
        product_type="15m",
    )
    assert cf is not None, "D-7: NULL position_size must NOT skip (only NULL entry_price skips)"
    assert isinstance(cf, int) and cf != 0, f"D-7: expected non-zero 1ct cf, got {cf!r}"
