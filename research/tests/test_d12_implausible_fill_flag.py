"""D-12 — IMPLAUSIBLE_FILL flag (>10% balance daily cf-PnL).

Authoritative source: scripts/alpha_audit.compute_top_opportunities (per RCA
D-12). Catches the `low_price_shadow` pathology where cf says +$944/day on a
$495 balance because cf assumes 100% fill at recorded ask without slippage
or depth modeling.

Flag fires when: daily_cf_pnl > 10% × available_balance_cents.
Edge cases:
- balance_cents IS NULL → balance_unknown=True (Round-2 alpha-audit-rebuild)
- balance_cents <= 0 → balance_unknown=True
- daily_cf_pnl is negative — flag does NOT fire (we're guarding against
  implausibly HIGH positive cf, not losses).

The flag is INFORMATIONAL — does NOT modify the cf number. Replay results
flowing into the autoresearch eval gate must auto-reject candidates with
implausible_fill=True.

TDD-red until B3 ships research.replay.implausible_fill_check (or equivalent).
"""
from __future__ import annotations

import pytest


def test_d12_replay_has_implausible_fill_check() -> None:
    """B3 must ship research.replay.implausible_fill_check (TDD-red)."""
    import research.replay as rep
    assert hasattr(rep, "implausible_fill_check"), (
        "D-12 TDD-red: B3 must ship research.replay.implausible_fill_check"
    )


def test_d12_flag_fires_above_10pct_threshold() -> None:
    """+$50 cf at $400 balance: 50 > 40 (10% of 400) → flag fires."""
    import research.replay as rep
    if not hasattr(rep, "implausible_fill_check"):
        pytest.skip("D-12 TDD-red: implausible_fill_check not yet implemented")
    result = rep.implausible_fill_check(daily_cf_pnl_cents=5000, balance_cents=40000)
    # 5000c ($50) > 0.10 × 40000c ($40) → True
    flag = result.get("implausible_fill", None) if isinstance(result, dict) else getattr(result, "implausible_fill", None)
    assert flag is True, f"D-12 flag: expected True (50 > 40), got {flag!r} from {result!r}"


def test_d12_flag_clear_below_threshold() -> None:
    """+$30 cf at $400 balance: 30 ≤ 40 → flag clear."""
    import research.replay as rep
    if not hasattr(rep, "implausible_fill_check"):
        pytest.skip("D-12 TDD-red: implausible_fill_check not yet implemented")
    result = rep.implausible_fill_check(daily_cf_pnl_cents=3000, balance_cents=40000)
    flag = result.get("implausible_fill", None) if isinstance(result, dict) else getattr(result, "implausible_fill", None)
    assert flag is False, f"D-12 flag: expected False (30 ≤ 40), got {flag!r}"


def test_d12_flag_at_exact_threshold() -> None:
    """Exactly 10%: 40 / 400 = 0.10. Pin the boundary behavior."""
    import research.replay as rep
    if not hasattr(rep, "implausible_fill_check"):
        pytest.skip("D-12 TDD-red: implausible_fill_check not yet implemented")
    # At exactly threshold: strict >, so 40 = 10% × 400 → NOT implausible
    result = rep.implausible_fill_check(daily_cf_pnl_cents=4000, balance_cents=40000)
    flag = result.get("implausible_fill", None) if isinstance(result, dict) else getattr(result, "implausible_fill", None)
    # Convention: STRICT greater-than. If B3 uses >=, this test needs to flip.
    assert flag is False, (
        f"D-12 boundary: 40 = 10% × 400 should NOT trigger (strict >). Got {flag!r}"
    )


def test_d12_balance_null_sets_balance_unknown() -> None:
    """balance_cents=None → balance_unknown=True (skip-and-flag parity with alpha_audit).

    R1 finding M8: alpha_audit at line ~391 SKIPS the gate when balance_unknown
    fires (flag: "IMPLAUSIBLE_FILL gate skipped"). The B2 contract is:
    when balance is unknown, set balance_unknown=True AND implausible_fill is
    None (the gate was skipped — neither True nor False).
    """
    import research.replay as rep
    if not hasattr(rep, "implausible_fill_check"):
        pytest.skip("D-12 TDD-red: implausible_fill_check not yet implemented")
    result = rep.implausible_fill_check(daily_cf_pnl_cents=5000, balance_cents=None)
    bu = result.get("balance_unknown", None) if isinstance(result, dict) else getattr(result, "balance_unknown", None)
    flag = result.get("implausible_fill", "missing") if isinstance(result, dict) else getattr(result, "implausible_fill", "missing")
    assert bu is True, (
        f"D-12 balance NULL: expected balance_unknown=True, got {bu!r} from {result!r}"
    )
    # The gate was skipped — implausible_fill must be None (not True, not False)
    assert flag is None, (
        f"D-12 balance NULL: expected implausible_fill=None (gate skipped per alpha_audit), "
        f"got {flag!r}. Skip-and-flag parity with alpha_audit:~391."
    )


def test_d12_balance_zero_sets_balance_unknown() -> None:
    """balance_cents=0 → balance_unknown=True (would otherwise divide by zero)."""
    import research.replay as rep
    if not hasattr(rep, "implausible_fill_check"):
        pytest.skip("D-12 TDD-red: implausible_fill_check not yet implemented")
    result = rep.implausible_fill_check(daily_cf_pnl_cents=5000, balance_cents=0)
    bu = result.get("balance_unknown", None) if isinstance(result, dict) else getattr(result, "balance_unknown", None)
    assert bu is True, f"D-12 balance=0: expected balance_unknown=True, got {bu!r}"


def test_d12_balance_negative_sets_balance_unknown() -> None:
    """balance_cents < 0 → balance_unknown=True (sentinel for unknown state)."""
    import research.replay as rep
    if not hasattr(rep, "implausible_fill_check"):
        pytest.skip("D-12 TDD-red: implausible_fill_check not yet implemented")
    result = rep.implausible_fill_check(daily_cf_pnl_cents=5000, balance_cents=-100)
    bu = result.get("balance_unknown", None) if isinstance(result, dict) else getattr(result, "balance_unknown", None)
    assert bu is True, f"D-12 negative balance: expected balance_unknown=True, got {bu!r}"


def test_d12_negative_daily_cf_does_not_trigger_flag() -> None:
    """Negative cf (a loss day) does NOT trigger the implausibly-high-fill flag.

    Pin the asymmetry: the flag guards against implausible positive cf
    (slippage-free 100% fill), not implausibly negative.
    """
    import research.replay as rep
    if not hasattr(rep, "implausible_fill_check"):
        pytest.skip("D-12 TDD-red: implausible_fill_check not yet implemented")
    # -$50 at $400 balance: |cf| > 10% threshold, but cf is negative
    result = rep.implausible_fill_check(daily_cf_pnl_cents=-5000, balance_cents=40000)
    flag = result.get("implausible_fill", None) if isinstance(result, dict) else getattr(result, "implausible_fill", None)
    assert flag is False, (
        f"D-12 negative cf: expected False (no flag on losses), got {flag!r}"
    )


def test_d12_flag_is_informational_does_not_modify_cf() -> None:
    """The flag does NOT modify the cf number — separate field in the result.

    Pin the contract: implausible_fill IS NOT a transform of cf_pnl. Replay
    returns BOTH the raw cf AND the flag; the autoresearch eval gate uses
    the flag to auto-reject, but cf itself stays canonical.
    """
    import research.replay as rep
    if not hasattr(rep, "implausible_fill_check"):
        pytest.skip("D-12 TDD-red: implausible_fill_check not yet implemented")
    # Function signature should accept cf + balance; output should preserve cf.
    result = rep.implausible_fill_check(daily_cf_pnl_cents=5000, balance_cents=40000)
    cf_echo = result.get("daily_cf_pnl_cents", None) if isinstance(result, dict) else getattr(result, "daily_cf_pnl_cents", None)
    if cf_echo is not None:
        assert cf_echo == 5000, (
            f"D-12 cf preservation: flag function modified cf from 5000 to {cf_echo!r}"
        )


def test_d12_threshold_constant_pinned() -> None:
    """The 10% threshold is pinned. If B3 changes it, this test surfaces.

    Documents the value lineage: 10% comes from alpha_audit Round-2 review.
    """
    import research.replay as rep
    if not hasattr(rep, "IMPLAUSIBLE_FILL_THRESHOLD"):
        pytest.skip("D-12 TDD-red: IMPLAUSIBLE_FILL_THRESHOLD constant not yet exposed")
    assert rep.IMPLAUSIBLE_FILL_THRESHOLD == 0.10, (
        f"D-12 threshold drift: expected 0.10, got {rep.IMPLAUSIBLE_FILL_THRESHOLD!r}"
    )
