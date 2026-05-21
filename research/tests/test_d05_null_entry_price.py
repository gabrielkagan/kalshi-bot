"""D-5 — NULL entry_price produces NULL cf.

Authoritative source: bot._impl::MainLoop::_poll_evaluated_opportunities cf branch
(per RCA D-5). When market_price IS NULL, live writes counterfactual_pnl=NULL and
counterfactual='unknown_no_price'. Replay must skip these rows — NULL in, NULL
out (NOT 0, NOT an exception).

This is the "missing-input → honest NULL" pattern. Replay must NEVER fabricate a
cf value when the canonical input is missing.
"""
from __future__ import annotations

import pytest

from research.replay import replay_cf_pnl


def test_d05_null_entry_price_returns_none() -> None:
    """NULL entry_price -> None (NOT 0, NOT exception)."""
    cf = replay_cf_pnl(
        entry_price=None,
        market_result="yes",
        side="yes",
        position_size=10,
        product_type="15m",
    )
    assert cf is None, f"D-5: NULL entry_price should return None, got {cf!r}"


@pytest.mark.parametrize("side", ["yes", "no", None])
@pytest.mark.parametrize("result", ["yes", "no", "all_yes", "all_no", None, "other"])
@pytest.mark.parametrize("product", ["15m", "weather", "spx_hourly", "hourly", None])
def test_d05_null_entry_price_universal(side: str, result: str, product: str) -> None:
    """NULL entry_price always returns None regardless of side/result/product.

    The NULL-input gate is the FIRST check in the cf path — every other
    branch downstream of it must be unreachable when entry_price is None.
    """
    cf = replay_cf_pnl(
        entry_price=None,
        market_result=result,
        side=side,
        position_size=1,
        product_type=product,
    )
    assert cf is None, (
        f"D-5: NULL entry should be None universally; "
        f"side={side!r} result={result!r} product={product!r} -> {cf!r}"
    )


def test_d05_zero_entry_is_not_null_path() -> None:
    """entry_price=0 is NOT the NULL path — separate divergence source.

    B1 shipped a stale-exclude for entry_price=0 (the cf=-62 anomaly,
    1-of-67 ratio). That row exists in the snapshot but its handling is
    out of scope for D-5. D-5 only covers `entry_price IS NULL` rows.
    Entry=0 returns 0 from the canonical formula ((100-0)*count*0 - fee=0),
    which is different from NULL.
    """
    cf = replay_cf_pnl(
        entry_price=0,
        market_result="yes",
        side="yes",
        position_size=1,
        product_type="15m",
    )
    # entry=0 WIN: (100-0)*1 - ceil(0.07*1*0*100/100) = 100 - 0 = 100
    assert cf == 100, f"D-5/B1 entry=0 boundary: expected 100, got {cf!r}"
    # entry=0 LOSS: -(0*1 + 0) = 0
    cf_loss = replay_cf_pnl(
        entry_price=0,
        market_result="no",
        side="yes",
        position_size=1,
        product_type="15m",
    )
    assert cf_loss == 0, f"D-5/B1 entry=0 LOSS boundary: expected 0, got {cf_loss!r}"
