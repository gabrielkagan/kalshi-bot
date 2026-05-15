"""DD-2 (bundled): _drawdown_scaler_readonly must mirror _drawdown_scaler
at the halt-floor.

Origin (ClickUp 86b9z6y4k bundled scope, 2026-05-15):
`PositionSizer._drawdown_scaler` returns 0.10 (floor) at halt — an
intentional "never fully lock out" policy added after a phantom-HWM
incident (see bot/models.py:1247-1251 warning). Its read-only twin
`_drawdown_scaler_readonly` still returns 0.0 at halt, predating that
change. This drift means dashboard/audit/DB-column consumers reading
the readonly see a different value from what the sizer actually applies.

This contract pins the two paths to agree at every regime: halt, quarter,
half, and full. The readonly may legitimately differ on early-out
guards (balance<=0, not _hwm_initialized) — the writer returns 1.0 in
those cases as a side-effect-free no-op, while the readonly is designed
to compute from `_balance_history` when populated regardless of the
passed-in arg. The lockstep applies only to the regime-tier branches.

Sister anchors:
  - bot/models.py::PositionSizer._drawdown_scaler (canonical writer)
  - bot/models.py::PositionSizer._drawdown_scaler_readonly (read-only twin)
  - bot/snapshots/dashboard_snapshot.py:382 (existing readonly consumer)
"""

from __future__ import annotations

import pytest

from bot.models import PositionSizer


@pytest.fixture
def primed_sizer() -> PositionSizer:
    """A PositionSizer with `_hwm_initialized=True` and a single balance
    history entry. Tests pass `balance_cents` to set the current ratio."""
    s = PositionSizer(starting_balance_cents=100_000)
    # Bypass warmup: set initialized=True and seed history directly.
    s._hwm_initialized = True
    return s


def _set_state(sizer: PositionSizer, *, balance_cents: int, hwm_cents: int) -> None:
    """Seed `_balance_history` with one entry at the target balance, and
    override the rolling HWM via the env-style override field."""
    import time
    sizer._balance_history.clear()
    sizer._balance_history.append((time.time(), balance_cents))
    sizer._override_hwm_cents = hwm_cents


@pytest.mark.parametrize(
    "ratio_label, balance, hwm",
    [
        ("halt", 60_000, 100_000),       # ratio=0.60 < halt 0.65
        ("quarter", 70_000, 100_000),    # ratio=0.70 < quarter 0.75
        ("half", 80_000, 100_000),       # ratio=0.80 < half 0.85
        ("full", 95_000, 100_000),       # ratio=0.95 >= all thresholds
    ],
)
def test_readonly_matches_writer_at_each_regime(
    primed_sizer: PositionSizer,
    ratio_label: str,
    balance: int,
    hwm: int,
) -> None:
    """At each drawdown regime, the readonly twin must agree with the writer."""
    _set_state(primed_sizer, balance_cents=balance, hwm_cents=hwm)
    write_val = primed_sizer._drawdown_scaler(balance)
    read_val = primed_sizer._drawdown_scaler_readonly(balance)
    assert read_val == write_val, (
        f"regime={ratio_label}: writer returned {write_val}, "
        f"readonly returned {read_val} — drift between sizing decision "
        f"and signal-display path. See ClickUp 86b9z6y4k bundled scope."
    )


def test_readonly_halt_floor_is_zero_point_one(primed_sizer: PositionSizer) -> None:
    """Explicit pin: at halt, readonly must return 0.10 (the floor),
    NEVER 0.0. The 0.0 value predates the floor and was a permanent-
    lockout bug fixed in the writer; the readonly twin was missed."""
    _set_state(primed_sizer, balance_cents=50_000, hwm_cents=100_000)  # ratio=0.50
    val = primed_sizer._drawdown_scaler_readonly(50_000)
    assert val == 0.10, (
        f"halt-floor readonly = {val}, expected 0.10. The writer at "
        f"bot/models.py:1247-1251 explicitly returns 0.10 floor (with "
        f"warning log) to prevent permanent lockout; the readonly must "
        f"mirror that. ClickUp 86b9z6y4k."
    )
