"""D-14 — PositionSizer state / drawdown scaler is stateful.

Authoritative source: bot._impl::PositionSizer (per RCA D-14). _balance_history
is a 7-day deque; _drawdown_scaler reads _balance_history[-1] (RECORDED
portfolio balance), NOT the balance_cents arg passed to compute().

Replay cannot just call PositionSizer.compute() once per row — it must:
1. Materialize _balance_history from chronologically-ordered balance snapshots.
2. Replay warmup: median of first 5, then spike rejection.
3. For each row, look up the appropriate HWM at that timestamp.

B2 scope (per plan): pin the SIMPLIFIED reconstruction identity. The
canonical balance-stream source (drawdown_scaler column vs separate stream)
is a B3 / Phase 1a open question — B2 tests the IDENTITY contract.

Drawdown tier identity (per RCA D-14 + concepts/drawdown-scaler.md):
- balance >= 85% × HWM → scaler = 1.00
- balance ∈ [75%, 85%) × HWM → scaler = 0.50
- balance ∈ [65%, 75%) × HWM → scaler = 0.25
- balance < 65% × HWM → scaler = 0.10 (the FLOOR, not 0.0)

TDD-red until B3 ships research.replay.drawdown_scaler.
"""
from __future__ import annotations

import pytest


def test_d14_replay_has_drawdown_scaler() -> None:
    """B3 must ship research.replay.drawdown_scaler (TDD-red)."""
    import research.replay as rep
    assert hasattr(rep, "drawdown_scaler"), (
        "D-14 TDD-red: B3 must ship research.replay.drawdown_scaler(balance, hwm)"
    )


@pytest.mark.parametrize("ratio,expected_scaler", [
    # ratio = balance / hwm
    (1.00, 1.00),  # At HWM
    (0.95, 1.00),  # Above 85%
    (0.85, 1.00),  # At 85% boundary (inclusive)
    (0.80, 0.50),  # In [75%, 85%)
    (0.75, 0.50),  # At 75% boundary (inclusive)
    (0.70, 0.25),  # In [65%, 75%)
    (0.65, 0.25),  # At 65% boundary (inclusive)
    (0.60, 0.10),  # Below 65% (floor)
    (0.50, 0.10),  # Far below
    (0.00, 0.10),  # Extreme — floor still applies
])
def test_d14_drawdown_scaler_tier_identity(ratio: float, expected_scaler: float) -> None:
    """drawdown_scaler(balance, hwm) returns the correct tier for each ratio.

    The boundary is inclusive on the high side per RCA D-14 (e.g., ratio=0.85
    → scaler=1.0). If B3 uses strict-greater-than, this test surfaces it.
    """
    import research.replay as rep
    if not hasattr(rep, "drawdown_scaler"):
        pytest.skip("D-14 TDD-red: drawdown_scaler not yet implemented")
    hwm = 1_000_000  # $10,000
    balance = int(hwm * ratio)
    actual = rep.drawdown_scaler(balance_cents=balance, hwm_cents=hwm)
    assert actual == expected_scaler, (
        f"D-14 tier: ratio={ratio:.2f} balance/hwm={balance}/{hwm}, "
        f"expected={expected_scaler}, got={actual}"
    )


def test_d14_halt_floor_is_010_not_zero() -> None:
    """Per RCA D-14: 'the floor, not 0.0'. Pin that the halt-floor is 0.10."""
    import research.replay as rep
    if not hasattr(rep, "drawdown_scaler"):
        pytest.skip("D-14 TDD-red: drawdown_scaler not yet implemented")
    scaler = rep.drawdown_scaler(balance_cents=100, hwm_cents=1_000_000)
    assert scaler == 0.10, f"D-14 floor: expected 0.10 (halt floor), got {scaler}"
    assert scaler != 0.0, "D-14 floor: must NOT be 0.0 (would zero out all sizing)"


def test_d14_hwm_zero_or_null_is_handled() -> None:
    """hwm=0 or None: must NOT divide by zero. Either raises or returns sentinel."""
    import research.replay as rep
    if not hasattr(rep, "drawdown_scaler"):
        pytest.skip("D-14 TDD-red: drawdown_scaler not yet implemented")
    try:
        result = rep.drawdown_scaler(balance_cents=50000, hwm_cents=0)
        # Acceptable: returns a sentinel (e.g., 1.0 default or None)
        assert result is None or 0 < result <= 1.0, (
            f"D-14 hwm=0: expected sentinel/None or 0<=x<=1.0, got {result!r}"
        )
    except (ZeroDivisionError, ValueError):
        # Acceptable: raises explicitly
        pass


def test_d14_balance_history_reconstruction_helper_exists() -> None:
    """B3 ships a helper to reconstruct balance history (TDD-red).

    Per plan: 'replay's _reconstruct_balance_history(snapshot) produces the
    same HWM at every row's evaluation_time as PositionSizer would.'

    Naming flex: accept either `_reconstruct_balance_history` (private) or
    `reconstruct_balance_history` (public).
    """
    import research.replay as rep
    has_helper = (
        hasattr(rep, "_reconstruct_balance_history")
        or hasattr(rep, "reconstruct_balance_history")
    )
    assert has_helper, (
        "D-14 TDD-red: B3 must ship _reconstruct_balance_history(snapshot) "
        "OR reconstruct_balance_history(snapshot)"
    )


def test_d14_hwm_window_is_7_days() -> None:
    """The HWM lookback window is 7 days.

    NOTE: there is NO `bot.constants.HWM_LOOKBACK` symbol (R1 finding: M3).
    The canonical 7-day window is documented in:
      - config.py::DRAWDOWN_* thresholds (lines ~153-155)
      - kb/concepts/drawdown-scaler.md
      - bot._impl::PositionSizer.get_rolling_hwm() implementation

    Pin the constant if B3 exposes it under any of the accepted names.
    """
    import research.replay as rep
    if hasattr(rep, "HWM_WINDOW_DAYS"):
        assert rep.HWM_WINDOW_DAYS == 7, (
            f"D-14 HWM window drift: expected 7 days, got {rep.HWM_WINDOW_DAYS}"
        )
    elif hasattr(rep, "HWM_LOOKBACK_DAYS"):
        assert rep.HWM_LOOKBACK_DAYS == 7
    else:
        pytest.skip("D-14 TDD-red: HWM_WINDOW_DAYS constant not yet exposed")
