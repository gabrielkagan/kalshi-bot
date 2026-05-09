"""Tests for research.holdouts (temporal + stratified splits)."""
from __future__ import annotations

from datetime import datetime, timedelta

from research.holdouts import (
    price_tier,
    stratified_split,
    temporal_split,
)
from research.scoring import TradeRecord


def _record(eval_t, asset="BTC", price=85, product="15m", **overrides):
    base = dict(
        evaluation_time=eval_t,
        settled_at=eval_t + timedelta(minutes=15),
        product=product,
        asset=asset,
        side="yes",
        entry_price_cents=price,
        contracts=10,
        cf_pnl_cents=100,
        filter_stage="candidate",
        market_result="yes",
        available_balance_cents=50_000,
    )
    base.update(overrides)
    return TradeRecord(**base)


# ── price_tier ────────────────────────────────────────────────────────


def test_price_tier_boundaries():
    assert price_tier(0) == "<80"
    assert price_tier(79) == "<80"
    assert price_tier(80) == "80-89"
    assert price_tier(89) == "80-89"
    assert price_tier(90) == "90-97"
    assert price_tier(97) == "90-97"
    assert price_tier(98) == "98-99"
    assert price_tier(99) == "98-99"


# ── temporal_split ────────────────────────────────────────────────────


def test_temporal_split_partitions_at_boundary():
    base = datetime(2026, 5, 1, 0, 0, 0)
    rs = [_record(base + timedelta(days=i)) for i in range(30)]
    in_sample, holdout = temporal_split(rs, holdout_days=14)
    # reference_now = max(eval_time) = base+29d. cutoff = base+15d.
    # holdout = days 15..29 inclusive (15 records).
    assert len(holdout) == 15
    assert len(in_sample) == 15
    assert all(r.evaluation_time >= base + timedelta(days=15) for r in holdout)
    assert all(r.evaluation_time < base + timedelta(days=15) for r in in_sample)


def test_temporal_split_explicit_now_clamps_upper_bound():
    """R1-finding-1: when reference_now is set smaller than corpus
    max, post-window rows are DROPPED, not silently included."""
    base = datetime(2026, 5, 1)
    rs = [_record(base + timedelta(days=i)) for i in range(30)]
    fixed_now = base + timedelta(days=20)
    in_sample, holdout = temporal_split(rs, holdout_days=7, reference_now=fixed_now)
    cutoff = fixed_now - timedelta(days=7)
    # holdout window = [fixed_now − 7d, fixed_now]
    for r in holdout:
        assert cutoff <= r.evaluation_time <= fixed_now
    for r in in_sample:
        assert r.evaluation_time < cutoff
    # Records past fixed_now (days 21..29) must be dropped from BOTH.
    dropped = [r for r in rs if r.evaluation_time > fixed_now]
    assert len(dropped) == 9   # days 21..29 inclusive
    for d in dropped:
        assert d not in holdout
        assert d not in in_sample


def test_temporal_split_empty():
    assert temporal_split([], holdout_days=14) == ([], [])


# ── stratified_split ──────────────────────────────────────────────────


def test_stratified_split_deterministic_under_same_seed():
    base = datetime(2026, 5, 1)
    rs = []
    for i in range(80):
        rs.append(_record(
            base + timedelta(minutes=i),
            asset=("BTC", "ETH", "SOL", "XRP")[i % 4],
            price=(85, 92)[i % 2],
        ))
    a_in, a_out = stratified_split(rs, 0.20, seed=42)
    b_in, b_out = stratified_split(rs, 0.20, seed=42)
    assert [r.evaluation_time for r in a_out] == [r.evaluation_time for r in b_out]
    assert [r.evaluation_time for r in a_in] == [r.evaluation_time for r in b_in]


def test_stratified_split_different_seed_changes_partition():
    base = datetime(2026, 5, 1)
    rs = [
        _record(base + timedelta(minutes=i), asset="BTC", price=85)
        for i in range(100)
    ]
    _, out_a = stratified_split(rs, 0.20, seed=1)
    _, out_b = stratified_split(rs, 0.20, seed=2)
    a = sorted(r.evaluation_time for r in out_a)
    b = sorted(r.evaluation_time for r in out_b)
    assert a != b


def test_stratified_split_strata_proportionality():
    """Each (asset, price_tier) cell should hold ~20% of its rows."""
    base = datetime(2026, 5, 1)
    rs = []
    # 4 strata × 50 rows each
    for asset in ("BTC", "ETH"):
        for price in (85, 95):
            for i in range(50):
                rs.append(_record(
                    base + timedelta(minutes=i),
                    asset=asset, price=price,
                ))
    in_sample, out = stratified_split(rs, 0.20, seed=42)
    # With ceil(50 × 0.20) = 10 per stratum × 4 strata → 40 holdout
    # out of 200 total, leaving 160 in-sample.
    assert len(out) == 40
    assert len(in_sample) == 160


def test_stratified_split_small_stratum_gets_one():
    """N=1 stratum still contributes one holdout row (ceil floor)."""
    base = datetime(2026, 5, 1)
    rs = [_record(base, asset="BTC", price=85)]
    in_sample, out = stratified_split(rs, 0.20, seed=42)
    assert len(out) == 1
    assert in_sample == []


def test_stratified_split_rejects_invalid_fraction():
    import pytest
    with pytest.raises(ValueError):
        stratified_split([], 0.0, seed=42)
    with pytest.raises(ValueError):
        stratified_split([], 1.0, seed=42)


def test_stratified_split_disjoint_and_complete():
    base = datetime(2026, 5, 1)
    rs = [
        _record(base + timedelta(minutes=i),
                asset=("BTC", "ETH")[i % 2],
                price=(85, 95)[i % 2])
        for i in range(40)
    ]
    in_sample, out = stratified_split(rs, 0.20, seed=42)
    union = sorted(r.evaluation_time for r in in_sample + out)
    expected = sorted(r.evaluation_time for r in rs)
    assert union == expected
    assert not (set(id(r) for r in in_sample) & set(id(r) for r in out))
