"""D-3 — side-aware win predicate matrix.

Authoritative source: bot._impl::MainLoop::_poll_evaluated_opportunities cf branch
(per RCA D-3). The win/loss predicate is:

    is_win  = (side == "yes" and result in ("yes", "all_yes")) \\
           or (side == "no"  and result in ("no",  "all_no"))
    is_loss = (side == "yes" and result in ("no",  "all_no"))  \\
           or (side == "no"  and result in ("yes", "all_yes"))

Anything else → cf=None (unknown_result). NULL side defaults to 'yes' (D-22).

Parity contract:
- alpha_audit.SIDE_AWARE_WIN_SQL is the SQL projection of this predicate; replay
  inherits the same semantics on the Python side.
- The bug class this guards (from alpha-audit-rebuild Round-3): flipping a NO-side
  row's result NO→YES must invert the win flag. If a code path assumes YES-side
  only, NO-side rows get classified backwards.

D-22 already covers NULL/case-normalization for `side`. D-3 focuses on the
hand-computed truth-table matrix over the 4 canonical results.
"""
from __future__ import annotations

import pytest

from research.replay import replay_cf_pnl


# Canonical truth table. Sign convention:
#   "win"     → cf > 0
#   "loss"    → cf < 0
#   "unknown" → cf is None
#
# At entry=85, 1ct: WIN cf = 14, LOSS cf = -86 (from B1 canonical math).
ENTRY = 85
SIZE = 1
WIN_CF = 14
LOSS_CF = -86

# (side, result, outcome_class)
MATRIX = [
    # YES side: yes/all_yes win, no/all_no loss
    ("yes", "yes",     "win"),
    ("yes", "all_yes", "win"),
    ("yes", "no",      "loss"),
    ("yes", "all_no",  "loss"),
    # NO side: no/all_no win, yes/all_yes loss
    ("no",  "yes",     "loss"),
    ("no",  "all_yes", "loss"),
    ("no",  "no",      "win"),
    ("no",  "all_no",  "win"),
    # Unknown results — None regardless of side
    ("yes", "other",       "unknown"),
    ("yes", "all_neutral", "unknown"),
    ("yes", None,          "unknown"),
    ("no",  "other",       "unknown"),
    ("no",  "all_neutral", "unknown"),
    ("no",  None,          "unknown"),
]


@pytest.mark.parametrize("side,result,outcome", MATRIX)
def test_d03_side_aware_win_predicate(side: str, result: object, outcome: str) -> None:
    """Replay cf classification follows the hand-computed truth table."""
    cf = replay_cf_pnl(
        entry_price=ENTRY,
        market_result=result,
        side=side,
        position_size=SIZE,
        product_type="15m",
    )
    if outcome == "win":
        assert cf == WIN_CF, (
            f"D-3 win: side={side!r} result={result!r} -> {cf!r}, expected {WIN_CF}"
        )
    elif outcome == "loss":
        assert cf == LOSS_CF, (
            f"D-3 loss: side={side!r} result={result!r} -> {cf!r}, expected {LOSS_CF}"
        )
    else:  # unknown
        assert cf is None, (
            f"D-3 unknown: side={side!r} result={result!r} -> {cf!r}, expected None"
        )


def test_d03_no_side_row_flipping_result_inverts_win() -> None:
    """A NO-side row's win flag inverts when the result flips NO→YES.

    Catches the bug class fixed in alpha-audit-rebuild Round-3: a code path that
    classified rows under YES-side semantics would misclassify NO-side rows.
    """
    # NO-side, result=no → WIN (cf > 0)
    cf_win = replay_cf_pnl(
        entry_price=ENTRY, market_result="no", side="no",
        position_size=SIZE, product_type="15m",
    )
    # Flip result NO→YES (everything else identical) → LOSS (cf < 0)
    cf_loss = replay_cf_pnl(
        entry_price=ENTRY, market_result="yes", side="no",
        position_size=SIZE, product_type="15m",
    )
    assert cf_win > 0, f"D-3 NO+no should be WIN: cf={cf_win}"
    assert cf_loss < 0, f"D-3 NO+yes should be LOSS: cf={cf_loss}"
    assert cf_win == WIN_CF and cf_loss == LOSS_CF, (
        f"D-3 flip-inverts-sign: expected WIN={WIN_CF} LOSS={LOSS_CF}, "
        f"got WIN={cf_win} LOSS={cf_loss}"
    )


def test_d03_yes_side_unaffected_by_no_predicate() -> None:
    """A YES-side row's win predicate is unchanged when the NO-side branch exists.

    Symmetry counterpart to test_d03_no_side_row_flipping_result_inverts_win.
    Pins that BOTH sides have functional predicates, not just YES.
    """
    cf_yes_win = replay_cf_pnl(
        entry_price=ENTRY, market_result="yes", side="yes",
        position_size=SIZE, product_type="15m",
    )
    cf_no_win = replay_cf_pnl(
        entry_price=ENTRY, market_result="no", side="no",
        position_size=SIZE, product_type="15m",
    )
    # Both should be WIN with identical magnitude (same entry, size, fees).
    assert cf_yes_win == cf_no_win == WIN_CF, (
        f"D-3 symmetry: YES-win={cf_yes_win} NO-win={cf_no_win}, expected both {WIN_CF}"
    )


def test_d03_all_yes_distinct_from_yes_only_in_settlement_state() -> None:
    """'yes' and 'all_yes' produce the same cf classification.

    These are distinct settlement states (yes = direct win, all_yes = aggregate
    YES win), but the cf branch treats them identically. Pin that any future
    refactor that tries to differentiate at this layer is caught.
    """
    cf_yes = replay_cf_pnl(
        entry_price=ENTRY, market_result="yes", side="yes",
        position_size=SIZE, product_type="15m",
    )
    cf_all_yes = replay_cf_pnl(
        entry_price=ENTRY, market_result="all_yes", side="yes",
        position_size=SIZE, product_type="15m",
    )
    assert cf_yes == cf_all_yes == WIN_CF
    cf_no = replay_cf_pnl(
        entry_price=ENTRY, market_result="no", side="yes",
        position_size=SIZE, product_type="15m",
    )
    cf_all_no = replay_cf_pnl(
        entry_price=ENTRY, market_result="all_no", side="yes",
        position_size=SIZE, product_type="15m",
    )
    assert cf_no == cf_all_no == LOSS_CF
