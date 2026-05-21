"""D-6 — unknown market_result produces NULL cf.

Authoritative source: bot._impl::MainLoop::_poll_evaluated_opportunities cf branch
(per RCA D-6). When market_result is settled but outside the 4-value vocabulary
{'yes', 'no', 'all_yes', 'all_no'}, live writes pnl=None and taker_fee=0.

The 4-value vocab is the contract; everything else is "unknown" and produces
honest-NULL cf. Catches:
    - 'unknown'        (legacy / settlement system glitch)
    - 'all_neutral'    (settlement tie / void — rare on Kalshi)
    - 'void'           (Kalshi voided market — also rare)
    - case variation   ('YES' vs 'yes' — replay lowercases)
    - leading/trailing whitespace (' yes ' — replay does NOT trim; pin this)
"""
from __future__ import annotations

import pytest

from research.replay import replay_cf_pnl


# (market_result_value, expected_cf_or_none, comment)
UNKNOWN_RESULTS = [
    ("all_neutral", None, "settlement tie / void category"),
    ("unknown", None, "legacy settlement-system glitch"),
    ("void", None, "voided market"),
    ("none", None, "string 'none' is not the None literal"),
    ("", None, "empty string"),
    ("YES_NO", None, "concatenation typo"),
]


@pytest.mark.parametrize("result,expected,_comment", UNKNOWN_RESULTS)
def test_d06_unknown_result_returns_none(result: str, expected: object, _comment: str) -> None:
    """Any market_result outside {yes, no, all_yes, all_no} returns None."""
    cf = replay_cf_pnl(
        entry_price=85,
        market_result=result,
        side="yes",
        position_size=1,
        product_type="15m",
    )
    assert cf is expected, (
        f"D-6 unknown-result divergence: result={result!r} -> {cf!r}, "
        f"expected {expected!r} ({_comment})"
    )


def test_d06_null_result_returns_none() -> None:
    """Python None for market_result returns None (handled before vocab check)."""
    cf = replay_cf_pnl(
        entry_price=85,
        market_result=None,
        side="yes",
        position_size=1,
        product_type="15m",
    )
    assert cf is None, f"D-6 None result: expected None, got {cf!r}"


@pytest.mark.parametrize("result", ["YES", "Yes", "yEs"])
def test_d06_case_insensitive_yes(result: str) -> None:
    """Replay lowercases market_result (per `result = (market_result or "").lower()`).

    Catches the bug where someone bypasses the lower() call and uppercase
    results silently become "unknown".
    """
    cf = replay_cf_pnl(
        entry_price=85,
        market_result=result,
        side="yes",
        position_size=1,
        product_type="15m",
    )
    # Win: (100-85)*1 - ceil(0.07*1*85*15/100) = 15 - 1 = 14
    assert cf == 14, (
        f"D-6 case sensitivity: result={result!r} should normalize to 'yes' -> 14, "
        f"got {cf!r}"
    )


def test_d06_whitespace_in_result_is_unknown() -> None:
    """Replay does NOT trim — `' yes '` is treated as unknown.

    Pins the contract: callers must clean their inputs. Replay only normalizes
    case, not whitespace. If this drifts, the test forces an explicit decision.
    """
    cf = replay_cf_pnl(
        entry_price=85,
        market_result=" yes ",
        side="yes",
        position_size=1,
        product_type="15m",
    )
    assert cf is None, (
        f"D-6 whitespace handling: ' yes ' is treated as unknown -> None, "
        f"got {cf!r}"
    )
