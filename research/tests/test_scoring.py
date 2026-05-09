"""Tests for research.scoring composite gate.

Covers each of the six gates plus verdict precedence + IO.
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

from research.regime import REGISTERED_DEFAULT_CUTOFF
from research.scoring import (
    TradeRecord,
    bootstrap_delta,
    cumulative_max_dd,
    evaluate,
    implausible_fill_buckets,
    latest_known_balance,
    load_records_from_jsonl,
    result_to_dict,
)


REPO_ROOT = Path(__file__).resolve().parents[2]


def _record(eval_t, *, product="15m", asset="BTC", price=85, contracts=10,
            cf_pnl_cents=100, filter_stage="candidate", side="yes",
            market_result="yes", balance=50_000, settled_at=None):
    return TradeRecord(
        evaluation_time=eval_t,
        settled_at=settled_at if settled_at is not None
        else eval_t + timedelta(minutes=15),
        product=product,
        asset=asset,
        side=side,
        entry_price_cents=price,
        contracts=contracts,
        cf_pnl_cents=cf_pnl_cents,
        filter_stage=filter_stage,
        market_result=market_result,
        available_balance_cents=balance,
    )


def _strong_corpus(n_per_product=200, base=None, days=20, products=("15m",),
                   assets=("BTC", "ETH", "SOL", "XRP"),
                   pnl_per_trade=50, balance=200_000, filter_stage="candidate"):
    """A corpus engineered to pass all gates: each (product, asset)
    has ≥150 trades so stratified 20% holdout has ≥30 (the floor).
    Each trade is +pnl_per_trade, no DD, no IMPLAUSIBLE_FILL bucket
    (per-day < threshold × balance).
    """
    if base is None:
        base = datetime(2026, 5, 1, 0, 0, 0)
    out = []
    for p in products:
        for asset in assets:
            for i in range(n_per_product):
                out.append(_record(
                    base + timedelta(hours=i % (days * 24)),
                    product=p, asset=asset, cf_pnl_cents=pnl_per_trade,
                    filter_stage=filter_stage, balance=balance,
                ))
    return out


# ── implausible_fill_buckets ──────────────────────────────────────────


def test_implausible_fill_fires_above_threshold():
    base = datetime(2026, 5, 1)
    rs = [_record(base, cf_pnl_cents=50)]   # 1d: +50c
    fired, balance_unknown = implausible_fill_buckets(rs, balance_cents=400)
    assert balance_unknown is False
    assert fired == [("15m", "2026-05-01")]


def test_implausible_fill_clear_below_threshold():
    base = datetime(2026, 5, 1)
    rs = [_record(base, cf_pnl_cents=30)]
    fired, balance_unknown = implausible_fill_buckets(rs, balance_cents=400)
    assert balance_unknown is False
    assert fired == []


def test_implausible_fill_aggregates_per_product_day():
    base = datetime(2026, 5, 1)
    rs = [
        _record(base, product="15m", cf_pnl_cents=21),
        _record(base, product="15m", cf_pnl_cents=20),
        _record(base, product="hourly", cf_pnl_cents=30),
    ]
    fired, _ = implausible_fill_buckets(rs, balance_cents=400)
    assert ("15m", "2026-05-01") in fired
    assert ("hourly", "2026-05-01") not in fired


def test_implausible_fill_balance_unknown_none():
    fired, balance_unknown = implausible_fill_buckets([], None)
    assert balance_unknown is True
    assert fired == []


def test_implausible_fill_balance_unknown_zero_or_negative():
    rs = [_record(datetime(2026, 5, 1), cf_pnl_cents=100)]
    _, bu_zero = implausible_fill_buckets(rs, 0)
    _, bu_neg = implausible_fill_buckets(rs, -1)
    assert bu_zero and bu_neg


def test_implausible_fill_threshold_tunable():
    base = datetime(2026, 5, 1)
    rs = [_record(base, cf_pnl_cents=50)]
    f10, _ = implausible_fill_buckets(rs, 400, threshold=0.10)
    assert f10
    f20, _ = implausible_fill_buckets(rs, 400, threshold=0.20)
    assert f20 == []


# ── cumulative_max_dd ─────────────────────────────────────────────────


def test_max_dd_monotonic_increasing_is_zero():
    base = datetime(2026, 5, 1)
    rs = [_record(base + timedelta(hours=i), cf_pnl_cents=10) for i in range(5)]
    assert cumulative_max_dd(rs) == 0


def test_max_dd_simple_drawdown():
    base = datetime(2026, 5, 1)
    rs = [
        _record(base + timedelta(hours=0), cf_pnl_cents=100),  # 100
        _record(base + timedelta(hours=1), cf_pnl_cents=50),   # 150 peak
        _record(base + timedelta(hours=2), cf_pnl_cents=-80),  # 70 → DD 80
        _record(base + timedelta(hours=3), cf_pnl_cents=20),   # 90 → DD 60
    ]
    assert cumulative_max_dd(rs) == 80


def test_max_dd_empty_corpus_is_zero():
    assert cumulative_max_dd([]) == 0


def test_max_dd_uses_settled_at_ordering():
    base = datetime(2026, 5, 1)
    a = _record(base, cf_pnl_cents=100,
                settled_at=base + timedelta(hours=1))
    b = _record(base, cf_pnl_cents=-80,
                settled_at=base + timedelta(hours=2))
    c = _record(base, cf_pnl_cents=20,
                settled_at=base + timedelta(hours=3))
    forward = cumulative_max_dd([a, b, c])
    reversed_ = cumulative_max_dd([c, b, a])
    assert forward == reversed_ == 80


# ── bootstrap_delta ───────────────────────────────────────────────────


def test_bootstrap_delta_strictly_better_candidate_passes():
    base = datetime(2026, 5, 1)
    cand = [_record(base + timedelta(days=d), cf_pnl_cents=100) for d in range(20)]
    base_rs = [_record(base + timedelta(days=d), cf_pnl_cents=10) for d in range(20)]
    delta, sigma = bootstrap_delta(cand, base_rs, n_resamples=500, seed=42)
    assert delta > 0
    assert delta > 2 * sigma


def test_bootstrap_delta_flat_fails_2sigma():
    base = datetime(2026, 5, 1)
    rs = [_record(base + timedelta(days=d), cf_pnl_cents=100 + (d % 5))
          for d in range(20)]
    delta, sigma = bootstrap_delta(rs, rs, n_resamples=500, seed=42)
    assert abs(delta) < 1e-9


def test_bootstrap_delta_seed_deterministic():
    base = datetime(2026, 5, 1)
    cand = [_record(base + timedelta(days=d), cf_pnl_cents=100) for d in range(10)]
    base_rs = [_record(base + timedelta(days=d), cf_pnl_cents=20) for d in range(10)]
    a = bootstrap_delta(cand, base_rs, n_resamples=200, seed=7)
    b = bootstrap_delta(cand, base_rs, n_resamples=200, seed=7)
    assert a == b


# ── latest_known_balance ──────────────────────────────────────────────


def test_latest_known_balance_picks_max_by_eval_time():
    base = datetime(2026, 5, 1)
    rs = [
        _record(base, balance=10_000),
        _record(base + timedelta(days=1), balance=20_000),
        _record(base + timedelta(days=2), balance=30_000),
    ]
    assert latest_known_balance(rs) == 30_000


def test_latest_known_balance_skips_null_zero_negative():
    base = datetime(2026, 5, 1)
    rs = [
        _record(base + timedelta(days=2), balance=None),
        _record(base + timedelta(days=1), balance=0),
        _record(base + timedelta(days=0), balance=-1),
        _record(base + timedelta(days=-1), balance=12_345),
    ]
    assert latest_known_balance(rs) == 12_345


def test_latest_known_balance_all_missing_returns_none():
    base = datetime(2026, 5, 1)
    rs = [_record(base, balance=None), _record(base, balance=0)]
    assert latest_known_balance(rs) is None


# ── load_records_from_jsonl ───────────────────────────────────────────


def test_load_records_from_jsonl(tmp_path):
    p = tmp_path / "corpus.jsonl"
    p.write_text(
        json.dumps({
            "evaluation_time": "2026-05-01T00:00:00",
            "settled_at": "2026-05-01T00:15:00",
            "product": "15m", "asset": "BTC", "side": "yes",
            "entry_price_cents": 85, "contracts": 10,
            "cf_pnl_cents": 100, "filter_stage": "candidate",
            "market_result": "yes", "available_balance_cents": 50000,
        }) + "\n"
        + json.dumps({
            "evaluation_time": "2026-05-02T00:00:00",
            "settled_at": "2026-05-02T00:15:00",
            "product": "15m", "asset": "ETH", "side": None,
            "entry_price_cents": 92, "contracts": 5,
            "cf_pnl_cents": -50, "filter_stage": "candidate",
            "market_result": None, "available_balance_cents": None,
        }) + "\n"
    )
    rs = load_records_from_jsonl(p)
    assert len(rs) == 2
    assert rs[0].asset == "BTC" and rs[0].available_balance_cents == 50000
    assert rs[1].side is None and rs[1].available_balance_cents is None


# ── evaluate gate semantics ───────────────────────────────────────────


def _eval_with_strong_corpus(**overrides):
    """Helper: strong baseline + strong candidate (uplift) → PROMOTE."""
    cand = _strong_corpus(pnl_per_trade=200)
    base = _strong_corpus(pnl_per_trade=20)
    return evaluate(
        cand, base,
        regime_cutoff=None,
        balance_cents=overrides.pop("balance_cents", 1_000_000_000),
        n_resamples=overrides.pop("n_resamples", 200),
        **overrides,
    )


def test_strong_candidate_is_promoted():
    r = _eval_with_strong_corpus()
    assert r.accepted is True, (r.verdict, r.reasons)
    assert r.verdict == "PROMOTE"


def test_balance_unknown_rejects_with_dedicated_verdict():
    """BALANCE_UNKNOWN must NOT silently skip the gate (D-12)."""
    cand = _strong_corpus(pnl_per_trade=200, balance=None)
    base = _strong_corpus(pnl_per_trade=20, balance=None)
    cand = [TradeRecord(**{**r.__dict__, "available_balance_cents": None})
            for r in cand]
    base = [TradeRecord(**{**r.__dict__, "available_balance_cents": None})
            for r in base]
    r = evaluate(cand, base, regime_cutoff=None,
                 balance_cents=None, n_resamples=200)
    assert r.accepted is False
    assert r.verdict == "REJECT_BALANCE_UNKNOWN"


def test_implausible_fill_only_yields_promote_with_fill_risk():
    """When IMPLAUSIBLE_FILL is the ONLY failure → soft verdict."""
    cand = _strong_corpus(pnl_per_trade=1500)
    base = _strong_corpus(pnl_per_trade=20)
    r = evaluate(
        cand, base, regime_cutoff=None, balance_cents=10_000,
        n_resamples=200,
    )
    assert r.accepted is False
    assert r.verdict == "PROMOTE_WITH_FILL_RISK", (r.verdict, r.reasons)
    assert any("IMPLAUSIBLE_FILL" in s for s in r.reasons)


def test_implausible_fill_with_other_failure_yields_hard_reject():
    """If another gate also fails, IMPLAUSIBLE_FILL is not the verdict.

    Construction: BTC dominant (+1500c × 200 trades) → IMPLAUSIBLE_FILL
    fires on small balance. ETH tiny-negative (-1c × 200 trades) →
    NEGATIVE_PNL fires. BTC+ETH bootstrap delta is dominated by BTC
    so bootstrap passes. dd_multiplier loosened to bypass DD gate.
    """
    base = datetime(2026, 5, 1)
    cand_btc = [_record(base + timedelta(hours=i), asset="BTC",
                        cf_pnl_cents=1500) for i in range(200)]
    cand_eth = [_record(base + timedelta(hours=200 + i), asset="ETH",
                        cf_pnl_cents=-1) for i in range(200)]
    base_btc = [_record(base + timedelta(hours=i), asset="BTC",
                        cf_pnl_cents=5) for i in range(200)]
    base_eth = [_record(base + timedelta(hours=200 + i), asset="ETH",
                        cf_pnl_cents=-1) for i in range(200)]
    r = evaluate(
        cand_btc + cand_eth, base_btc + base_eth,
        regime_cutoff=None, balance_cents=10_000,
        n_resamples=200, dd_multiplier=10.0,
    )
    assert r.accepted is False
    assert r.verdict == "REJECT_PRODUCT_NEGATIVE_PNL", (r.verdict, r.reasons)
    assert any("IMPLAUSIBLE_FILL" in f for f in r.flags)


def test_per_product_floor_below_30_rejects():
    """29 candidate trades — below the 30 floor. Spread 1/day over
    29 days with varying per-day cf_pnl so both temporal and
    stratified holdouts have non-zero day-variance (bootstrap gate
    passes); the only failure is FLOOR. R2-1 made
    constant-delta corpora REJECT_BOOTSTRAP_BELOW_2SIGMA — must
    engineer real variance to isolate the FLOOR gate."""
    base = datetime(2026, 5, 1)
    cand = [_record(base + timedelta(days=i), asset="BTC",
                    cf_pnl_cents=100 + 50 * (i % 5)) for i in range(29)]
    base_rs = [_record(base + timedelta(days=i), asset="BTC",
                       cf_pnl_cents=10 + 5 * (i % 5)) for i in range(29)]
    r = evaluate(cand, base_rs, regime_cutoff=None,
                 balance_cents=1_000_000_000, n_resamples=200)
    assert r.accepted is False
    assert r.verdict == "REJECT_PRODUCT_TRADE_FLOOR", (r.verdict, r.reasons)


def test_per_product_floor_at_30_passes():
    base = datetime(2026, 5, 1)
    cand = []
    base_rs = []
    for i in range(200):
        d = base + timedelta(hours=i * 1)
        cand.append(_record(d, asset="BTC", cf_pnl_cents=200))
        base_rs.append(_record(d, asset="BTC", cf_pnl_cents=20))
    r = evaluate(cand, base_rs, regime_cutoff=None,
                 balance_cents=1_000_000_000, n_resamples=200)
    assert r.verdict != "REJECT_PRODUCT_TRADE_FLOOR", r.reasons


def test_per_product_negative_pnl_rejects():
    base = datetime(2026, 5, 1)
    cand_btc = [_record(base + timedelta(hours=i), asset="BTC",
                        cf_pnl_cents=200) for i in range(200)]
    cand_eth = [_record(base + timedelta(hours=200 + i), asset="ETH",
                        cf_pnl_cents=-1) for i in range(200)]
    base_btc = [_record(base + timedelta(hours=i), asset="BTC",
                        cf_pnl_cents=20) for i in range(200)]
    base_eth = [_record(base + timedelta(hours=200 + i), asset="ETH",
                        cf_pnl_cents=-1) for i in range(200)]
    r = evaluate(cand_btc + cand_eth, base_btc + base_eth,
                 regime_cutoff=None, balance_cents=1_000_000_000,
                 n_resamples=200, dd_multiplier=10.0)
    assert r.accepted is False
    assert r.verdict == "REJECT_PRODUCT_NEGATIVE_PNL", (r.verdict, r.reasons)


def test_max_dd_bound_rejects_when_candidate_drawdown_too_deep():
    base = datetime(2026, 5, 1)
    # Both corpora stay net-positive; candidate has 2× the baseline's DD
    # at the same offset. With dd_multiplier=1.10 strict, candidate's
    # 100c DD violates 1.10 × 50c baseline DD = 55c.
    # dd_bound_balance_floor=0 bypasses the absolute-floor (R1-finding-5)
    # so this test exercises the multiplier branch in isolation.
    cand = [_record(base + timedelta(hours=i), asset="BTC",
                    cf_pnl_cents=200) for i in range(199)]
    cand.append(_record(base + timedelta(hours=199), asset="BTC",
                        cf_pnl_cents=-100))
    base_rs = [_record(base + timedelta(hours=i), asset="BTC",
                       cf_pnl_cents=20) for i in range(199)]
    base_rs.append(_record(base + timedelta(hours=199), asset="BTC",
                           cf_pnl_cents=-50))
    r = evaluate(cand, base_rs, regime_cutoff=None,
                 balance_cents=1_000_000_000, n_resamples=200,
                 dd_bound_balance_floor=0.0)
    assert r.accepted is False
    assert r.verdict == "REJECT_DD_BOUND", (r.verdict, r.reasons)


def test_regime_cutoff_drops_pre_cutoff_rows_from_both_corpora():
    base_pre = datetime(2026, 1, 1)
    base_post = datetime(2026, 5, 1)
    cand = (
        [_record(base_pre + timedelta(hours=i), cf_pnl_cents=100)
         for i in range(50)]
        + [_record(base_post + timedelta(hours=i), cf_pnl_cents=200)
           for i in range(40)]
    )
    base_rs = (
        [_record(base_pre + timedelta(hours=i), cf_pnl_cents=10)
         for i in range(50)]
        + [_record(base_post + timedelta(hours=i), cf_pnl_cents=20)
           for i in range(40)]
    )
    r = evaluate(
        cand, base_rs, regime_cutoff=base_post,
        balance_cents=1_000_000_000, n_resamples=200,
    )
    assert r.per_holdout["temporal"].n_trades_baseline <= 40
    assert r.per_holdout["temporal"].n_trades_candidate <= 40


def test_cell_block_union_excludes_block_tier_rows():
    """BLOCK-tier rows must NOT count toward candidate aggregates."""
    base = datetime(2026, 5, 1)
    cand_block_rows = [
        _record(base + timedelta(hours=i), asset="BTC",
                cf_pnl_cents=10_000_000,
                filter_stage="TM98_97_98C_2_5MIN_BLEED")
        for i in range(20)
    ]
    cand_real = [
        _record(base + timedelta(hours=100 + i), asset="BTC",
                cf_pnl_cents=200, filter_stage="candidate")
        for i in range(40)
    ]
    base_rs = [
        _record(base + timedelta(hours=i), asset="BTC", cf_pnl_cents=20)
        for i in range(40)
    ]
    r = evaluate(
        cand_block_rows + cand_real, base_rs,
        regime_cutoff=None, balance_cents=1_000_000_000,
        n_resamples=200,
    )
    assert r.per_holdout["temporal"].n_trades_candidate <= 40
    assert (
        r.per_holdout["temporal"].pnl_per_product_candidate.get("15m/BTC", 0)
        < 10_000_000
    )


# ── result_to_dict serialization ──────────────────────────────────────


def test_result_to_dict_is_json_safe():
    r = _eval_with_strong_corpus()
    d = result_to_dict(r)
    s = json.dumps(d, default=str)
    parsed = json.loads(s)
    assert parsed["accepted"] == r.accepted
    assert parsed["verdict"] == r.verdict


# ── CLI smoke ─────────────────────────────────────────────────────────


def _write_corpus(path, records):
    with open(path, "w") as fh:
        for r in records:
            fh.write(json.dumps({
                "evaluation_time": r.evaluation_time.isoformat(),
                "settled_at": r.settled_at.isoformat(),
                "product": r.product, "asset": r.asset, "side": r.side,
                "entry_price_cents": r.entry_price_cents,
                "contracts": r.contracts,
                "cf_pnl_cents": r.cf_pnl_cents,
                "filter_stage": r.filter_stage,
                "market_result": r.market_result,
                "available_balance_cents": r.available_balance_cents,
            }) + "\n")


def test_cli_smoke_accepted_returns_zero(tmp_path):
    cand_path = tmp_path / "cand.jsonl"
    base_path = tmp_path / "base.jsonl"
    _write_corpus(cand_path,
                  _strong_corpus(pnl_per_trade=200, balance=1_000_000_000))
    _write_corpus(base_path,
                  _strong_corpus(pnl_per_trade=20, balance=1_000_000_000))
    res = subprocess.run(
        [sys.executable, "-m", "research.eval",
         "--corpus", str(cand_path),
         "--baseline", str(base_path),
         "--regime-cutoff", "none",
         "--balance-cents", "1000000000",
         "--n-resamples", "200",
         "--quiet"],
        cwd=str(REPO_ROOT), capture_output=True, text=True,
    )
    assert res.returncode == 0, (res.stdout, res.stderr)
    assert "PROMOTE" in res.stdout


def test_cli_smoke_rejected_returns_one(tmp_path):
    base = datetime(2026, 5, 1)
    cand = [_record(base + timedelta(hours=i), cf_pnl_cents=200)
            for i in range(5)]
    base_rs = [_record(base + timedelta(hours=i), cf_pnl_cents=20)
               for i in range(5)]
    cand_path = tmp_path / "c.jsonl"
    base_path = tmp_path / "b.jsonl"
    _write_corpus(cand_path, cand)
    _write_corpus(base_path, base_rs)
    res = subprocess.run(
        [sys.executable, "-m", "research.eval",
         "--corpus", str(cand_path),
         "--baseline", str(base_path),
         "--regime-cutoff", "none",
         "--balance-cents", "1000000000",
         "--n-resamples", "100",
         "--quiet"],
        cwd=str(REPO_ROOT), capture_output=True, text=True,
    )
    assert res.returncode == 1, (res.stdout, res.stderr)


def test_cli_warns_on_disabled_regime_cutoff(tmp_path):
    cand = _strong_corpus(pnl_per_trade=200)
    base = _strong_corpus(pnl_per_trade=20)
    cand_path = tmp_path / "c.jsonl"
    base_path = tmp_path / "b.jsonl"
    _write_corpus(cand_path, cand)
    _write_corpus(base_path, base)
    res = subprocess.run(
        [sys.executable, "-m", "research.eval",
         "--corpus", str(cand_path),
         "--baseline", str(base_path),
         "--regime-cutoff", "none",
         "--balance-cents", "1000000000",
         "--n-resamples", "100",
         "--quiet"],
        cwd=str(REPO_ROOT), capture_output=True, text=True,
    )
    assert "WARNING" in res.stderr
    assert "regime cutoff DISABLED" in res.stderr


# ── R1 regression tests (one per finding) ─────────────────────────────


def test_r1_finding_1_temporal_split_uses_shared_reference_now(tmp_path):
    """R1-1: cand goes 30 days, baseline 25 days. Without the
    shared_now fix, cand_holdout = days 16..30 and base_holdout =
    days 11..25, only 10 of 15 overlap. With shared_now =
    min(max_cand, max_base) = day 25, both holdouts cover days 11..25.
    """
    base = datetime(2026, 5, 1)
    cand = [_record(base + timedelta(days=d), cf_pnl_cents=100,
                    asset="BTC") for d in range(30)]
    base_rs = [_record(base + timedelta(days=d), cf_pnl_cents=100,
                       asset="BTC") for d in range(25)]
    r = evaluate(cand, base_rs, regime_cutoff=None,
                 balance_cents=1_000_000_000, n_resamples=200,
                 holdout_days=14)
    cand_dates = sorted(set(
        k.split("|")[1]
        for k in r.per_holdout["temporal"].daily_cf_pnl_buckets.keys()
    ))
    # shared_now = min(cand_max=base+29d, base_max=base+24d) = base+24d
    # = 2026-05-25. Cutoff = 2026-05-25 − 14d = 2026-05-11. Both holdouts
    # cover [2026-05-11, 2026-05-25]; without the fix, candidate window
    # would have stretched to 2026-05-30.
    assert cand_dates[0] >= "2026-05-11", cand_dates
    assert cand_dates[-1] <= "2026-05-25", cand_dates


def test_r1_finding_2_no_reject_implausible_fill_verdict_emitted():
    """R1-2: REJECT_IMPLAUSIBLE_FILL is impossible to produce. Any
    IMPLAUSIBLE_FILL bucket either coincides with another hard
    reject (→ that verdict, with IMPLAUSIBLE_FILL in `flags`) or is
    the only failure (→ PROMOTE_WITH_FILL_RISK). Probe every
    combination by sweeping which gates fail and verify the verdict
    is never the literal REJECT_IMPLAUSIBLE_FILL string."""
    base = datetime(2026, 5, 1)

    # Combo 1: IMPLAUSIBLE_FILL alone.
    cand = _strong_corpus(pnl_per_trade=1500)
    base_rs = _strong_corpus(pnl_per_trade=20)
    r = evaluate(cand, base_rs, regime_cutoff=None,
                 balance_cents=10_000, n_resamples=100)
    assert r.verdict != "REJECT_IMPLAUSIBLE_FILL"

    # Combo 2: IMPLAUSIBLE_FILL + NEGATIVE_PNL → NEGATIVE_PNL wins.
    cand_btc = [_record(base + timedelta(hours=i), asset="BTC",
                        cf_pnl_cents=1500) for i in range(200)]
    cand_eth = [_record(base + timedelta(hours=200 + i), asset="ETH",
                        cf_pnl_cents=-1) for i in range(200)]
    base_btc = [_record(base + timedelta(hours=i), asset="BTC",
                        cf_pnl_cents=5) for i in range(200)]
    base_eth = [_record(base + timedelta(hours=200 + i), asset="ETH",
                        cf_pnl_cents=-1) for i in range(200)]
    r = evaluate(cand_btc + cand_eth, base_btc + base_eth,
                 regime_cutoff=None, balance_cents=10_000,
                 n_resamples=200, dd_multiplier=10.0)
    assert r.verdict != "REJECT_IMPLAUSIBLE_FILL"


def test_r1_finding_3_cli_exit_codes_distinguish_error_from_reject(tmp_path):
    """R1-3: REJECT vs corpus-parse-error must be distinguishable
    via exit code so sweep harnesses can route errors differently."""
    # Missing corpus → 66 (EX_NOINPUT)
    res = subprocess.run(
        [sys.executable, "-m", "research.eval",
         "--corpus", str(tmp_path / "missing.jsonl"),
         "--baseline", str(tmp_path / "missing.jsonl"),
         "--regime-cutoff", "none", "--quiet"],
        cwd=str(REPO_ROOT), capture_output=True, text=True,
    )
    assert res.returncode == 66, (res.returncode, res.stderr)

    # Malformed JSON → 65 (EX_DATAERR)
    bad = tmp_path / "bad.jsonl"
    bad.write_text("not json\n")
    res = subprocess.run(
        [sys.executable, "-m", "research.eval",
         "--corpus", str(bad), "--baseline", str(bad),
         "--regime-cutoff", "none", "--quiet"],
        cwd=str(REPO_ROOT), capture_output=True, text=True,
    )
    assert res.returncode == 65, (res.returncode, res.stderr)


def test_r1_finding_4_stratified_split_documents_non_paired_limitation():
    """R1-4: docstring honesty — same-seed stratified split does
    NOT produce row-identical holdouts when corpora populations
    differ. Demonstrate empirically + lock the limitation."""
    base = datetime(2026, 5, 1)
    cand = [_record(base + timedelta(hours=i), asset="BTC",
                    cf_pnl_cents=100) for i in range(30)]
    base_rs = [_record(base + timedelta(hours=i), asset="BTC",
                       cf_pnl_cents=100) for i in range(40)]
    from research.holdouts import stratified_split
    _, c_hold = stratified_split(cand, 0.20, seed=42)
    _, b_hold = stratified_split(base_rs, 0.20, seed=42)
    c_eval = sorted(r.evaluation_time for r in c_hold)
    b_eval = sorted(r.evaluation_time for r in b_hold)
    # If row-identical, every cand-holdout time would be in
    # base-holdout. With asymmetric populations, this is false.
    assert not set(c_eval).issubset(set(b_eval)), (
        "stratified split is NOT actually row-identical across "
        "asymmetric corpora — docstring + plan doc must reflect this"
    )


def test_r1_finding_5_dd_floor_protects_near_zero_baseline_dd():
    """R1-5: when baseline_max_dd ≈ 0, DD bound = max(0, balance ×
    0.005). A candidate with tiny absolute DD must NOT trip DD-bound
    just because baseline happens to be monotonic-up."""
    base = datetime(2026, 5, 1)
    cand = [_record(base + timedelta(hours=i), asset="BTC",
                    cf_pnl_cents=200) for i in range(200)]
    # Insert one small dip → DD = 50c.
    cand[100] = _record(base + timedelta(hours=100), asset="BTC",
                        cf_pnl_cents=-50)
    base_rs = [_record(base + timedelta(hours=i), asset="BTC",
                       cf_pnl_cents=20) for i in range(200)]   # monotonic
    # Balance × 0.005 = 50000 × 0.005 = 250c floor → 50c < 250c → passes.
    r = evaluate(cand, base_rs, regime_cutoff=None,
                 balance_cents=50_000, n_resamples=200)
    assert r.verdict != "REJECT_DD_BOUND", (r.verdict, r.reasons)


def test_r1_finding_6_empty_corpus_returns_dedicated_verdict():
    """R1-6: empty candidate or baseline → REJECT_INSUFFICIENT_CORPUS
    with input sizes in `reasons`. NOT silently downstream-rejected
    via DD or other gates."""
    base = datetime(2026, 5, 1)
    rs = [_record(base + timedelta(hours=i), cf_pnl_cents=200)
          for i in range(40)]
    r = evaluate([], rs, regime_cutoff=None,
                 balance_cents=1_000_000_000, n_resamples=100)
    assert r.verdict == "REJECT_INSUFFICIENT_CORPUS"
    assert "candidate" in " ".join(r.reasons).lower()

    r = evaluate(rs, [], regime_cutoff=None,
                 balance_cents=1_000_000_000, n_resamples=100)
    assert r.verdict == "REJECT_INSUFFICIENT_CORPUS"


def test_r1_finding_6_all_block_tier_candidate_returns_insufficient_corpus():
    """If every candidate row is BLOCK-tier (filtered out by
    cell-block UNION), candidate is effectively empty and we should
    see REJECT_INSUFFICIENT_CORPUS, not silent zero-counts."""
    base = datetime(2026, 5, 1)
    cand = [_record(base + timedelta(hours=i), asset="BTC",
                    cf_pnl_cents=10000,
                    filter_stage="TM98_97_98C_2_5MIN_BLEED")
            for i in range(50)]
    rs = [_record(base + timedelta(hours=i), cf_pnl_cents=20)
          for i in range(50)]
    r = evaluate(cand, rs, regime_cutoff=None,
                 balance_cents=1_000_000_000, n_resamples=100)
    assert r.verdict == "REJECT_INSUFFICIENT_CORPUS", (r.verdict, r.reasons)


# ── R2 regression tests ───────────────────────────────────────────────


def test_r2_finding_1_zero_sigma_with_positive_delta_fails_gate():
    """R2-1: single-day candidate produces sigma=0; the previous
    `if delta <= 0: fail` masked the auto-pass on positive delta.
    Now zero-sigma always fails the bootstrap gate explicitly."""
    base = datetime(2026, 5, 1)
    # All trades on a single day → one bootstrap day → sigma=0.
    cand = [_record(base + timedelta(minutes=i), asset="BTC",
                    cf_pnl_cents=200) for i in range(200)]
    base_rs = [_record(base + timedelta(minutes=i), asset="BTC",
                       cf_pnl_cents=20) for i in range(200)]
    r = evaluate(cand, base_rs, regime_cutoff=None,
                 balance_cents=1_000_000_000, n_resamples=200)
    assert r.accepted is False, (r.verdict, r.reasons)
    assert r.verdict == "REJECT_BOOTSTRAP_BELOW_2SIGMA"
    assert any("insufficient day-variance" in s for s in r.reasons)


def test_r2_finding_2_aware_timestamp_normalized_to_naive_utc(tmp_path):
    """R2-2: corpus with `+00:00` timestamps must not raise
    TypeError when compared against the naive default cutoff."""
    p = tmp_path / "aware.jsonl"
    p.write_text(json.dumps({
        "evaluation_time": "2026-05-15T12:00:00+00:00",
        "settled_at": "2026-05-15T12:15:00+00:00",
        "product": "15m", "asset": "BTC", "side": "yes",
        "entry_price_cents": 85, "contracts": 10,
        "cf_pnl_cents": 100, "filter_stage": "candidate",
        "market_result": "yes", "available_balance_cents": 50000,
    }) + "\n")
    rs = load_records_from_jsonl(p)
    assert rs[0].evaluation_time.tzinfo is None
    assert rs[0].evaluation_time.year == 2026


def test_r2_finding_2_null_int_field_does_not_raise(tmp_path):
    """R2-2: `available_balance_cents: null` must coerce to None,
    not raise TypeError on `int(None)`."""
    p = tmp_path / "null_balance.jsonl"
    p.write_text(json.dumps({
        "evaluation_time": "2026-05-15T12:00:00",
        "settled_at": "2026-05-15T12:15:00",
        "product": "15m", "asset": "BTC", "side": "yes",
        "entry_price_cents": 85, "contracts": 10,
        "cf_pnl_cents": 100, "filter_stage": "candidate",
        "market_result": "yes", "available_balance_cents": None,
    }) + "\n")
    rs = load_records_from_jsonl(p)
    assert rs[0].available_balance_cents is None


def test_r2_finding_2_aware_corpus_via_cli_routes_to_dataerr(tmp_path):
    """R2-2 e2e: aware-timestamp corpus loaded via CLI scores
    successfully (does NOT crash). Verifies the eval.py exception
    handler covers TypeError from datetime mismatch."""
    p = tmp_path / "aware.jsonl"
    p.write_text(json.dumps({
        "evaluation_time": "2026-05-15T12:00:00+00:00",
        "settled_at": "2026-05-15T12:15:00+00:00",
        "product": "15m", "asset": "BTC", "side": "yes",
        "entry_price_cents": 85, "contracts": 10,
        "cf_pnl_cents": 100, "filter_stage": "candidate",
        "market_result": "yes", "available_balance_cents": 50000,
    }) + "\n")
    res = subprocess.run(
        [sys.executable, "-m", "research.eval",
         "--corpus", str(p), "--baseline", str(p),
         "--regime-cutoff", "none", "--quiet",
         "--balance-cents", "1000000000"],
        cwd=str(REPO_ROOT), capture_output=True, text=True,
    )
    # corpus is too small for a real verdict → likely
    # REJECT_INSUFFICIENT_HOLDOUT or similar, but exit must be 1
    # (clean reject), not 70 (software bug from TypeError).
    assert res.returncode in (0, 1), (res.returncode, res.stderr)


def test_r2_finding_3_dd_floor_only_when_baseline_dd_is_zero():
    """R2-3: DD bound uses the multiplier whenever baseline has
    any DD signal, regardless of balance. Only when baseline_max_dd
    is exactly 0 does the balance-floor kick in."""
    base = datetime(2026, 5, 1)
    # Baseline has 50c DD (one trade dip). Candidate has 100c DD.
    # Old behavior: bound = max(55, 1B × 0.005 = 5M) = 5M → false-pass.
    # New behavior: baseline_dd > 0 → bound = 50 × 1.10 = 55 → fail.
    cand = [_record(base + timedelta(hours=i), asset="BTC",
                    cf_pnl_cents=200) for i in range(199)]
    cand.append(_record(base + timedelta(hours=199), asset="BTC",
                        cf_pnl_cents=-100))
    base_rs = [_record(base + timedelta(hours=i), asset="BTC",
                       cf_pnl_cents=20) for i in range(199)]
    base_rs.append(_record(base + timedelta(hours=199), asset="BTC",
                           cf_pnl_cents=-50))
    r = evaluate(cand, base_rs, regime_cutoff=None,
                 balance_cents=1_000_000_000, n_resamples=200)
    assert r.verdict == "REJECT_DD_BOUND", (r.verdict, r.reasons)


def test_r2_finding_4_block_filter_symmetric_on_baseline():
    """R2-4: BLOCK-tier rows in baseline must be filtered out of
    baseline aggregates, mirroring candidate-side filtering.

    R3-finding-2: this test was originally load-non-bearing because
    BLOCK rows past candidate's max eval_time get pre-empted by
    `temporal_split`'s upper-bound clamp. Fix: interleave BLOCK
    rows WITHIN the candidate's time window so the clamp doesn't
    do the test's job for it. Also assert on stratified holdout
    (which doesn't honor the time clamp) for double coverage.
    """
    base = datetime(2026, 5, 1)
    cand = [_record(base + timedelta(hours=i), asset="BTC",
                    cf_pnl_cents=100) for i in range(200)]
    # 200 candidate baseline rows interleaved with 50 BLOCK rows
    # at half-hour offsets WITHIN the candidate window.
    base_rs = (
        [_record(base + timedelta(hours=i), asset="BTC",
                 cf_pnl_cents=20) for i in range(200)]
        + [_record(base + timedelta(hours=i, minutes=30), asset="BTC",
                   cf_pnl_cents=10000,
                   filter_stage="TM98_97_98C_2_5MIN_BLEED")
           for i in range(50)]
    )
    r = evaluate(cand, base_rs, regime_cutoff=None,
                 balance_cents=1_000_000_000, n_resamples=200)
    # Without symmetric filtering: temporal baseline would include
    # 50 BLOCK rows at +10000c = +500_000c phantom volume.
    # With R2-4 fix: only the 200 candidate baseline rows count.
    expected_max_baseline_pnl = 200 * 20   # = 4000c (allow some)
    assert r.per_holdout["temporal"].baseline_pnl_cents <= (
        expected_max_baseline_pnl + 1000
    ), r.per_holdout["temporal"].baseline_pnl_cents
    # Same on stratified (independent of temporal upper-bound clamp).
    assert r.per_holdout["stratified"].baseline_pnl_cents <= (
        expected_max_baseline_pnl + 1000
    ), r.per_holdout["stratified"].baseline_pnl_cents


def test_r2_finding_5_empty_holdout_yields_dedicated_verdict():
    """R2-5: empty holdout (corpus too old / shared_now too
    restrictive) returns REJECT_INSUFFICIENT_HOLDOUT, not a
    misleading bootstrap rejection."""
    base = datetime(2020, 1, 1)   # ancient corpus
    cand = [_record(base + timedelta(hours=i), asset="BTC",
                    cf_pnl_cents=200) for i in range(200)]
    # holdout_days=14, reference_now=base + 199h ~ base + 8d → cutoff
    # = base − 6d. All rows >= cutoff → all in holdout. NOT empty
    # in this construction. Need a different setup: regime_cutoff
    # past corpus end → both regime-filtered to empty → corpus gate.
    # Instead: stratified_fraction so high it leaves 0 in_sample but
    # holdout still has rows. That doesn't trigger empty either.
    # A real "empty holdout" requires reference_now WAY past corpus
    # so the trailing window has no rows at all.
    fixed_now = datetime(2026, 5, 1)   # 6 years past corpus end
    from research.holdouts import temporal_split
    _, h = temporal_split(cand, holdout_days=14, reference_now=fixed_now)
    assert h == []   # confirm setup yields empty temporal holdout

    # Now exercise via evaluate by skewing one corpus way past the
    # other. Cand at 2020, base at 2026 → shared_now = 2020 corpus end.
    # Base's holdout window [2020−14d, 2020] has no base rows → empty
    # base holdout → INSUFFICIENT_HOLDOUT.
    cand_ancient = cand
    base_modern = [_record(datetime(2026, 5, 1) + timedelta(hours=i),
                           asset="BTC", cf_pnl_cents=20)
                   for i in range(200)]
    r = evaluate(cand_ancient, base_modern, regime_cutoff=None,
                 balance_cents=1_000_000_000, n_resamples=100)
    assert r.verdict == "REJECT_INSUFFICIENT_HOLDOUT", (r.verdict, r.reasons)


def test_r2_finding_6_holdouts_non_independent_flag_present_on_promote():
    """R2-6: the HOLDOUTS_NON_INDEPENDENT flag is surfaced on every
    PROMOTE so downstream consumers know the AND is correlated
    evidence."""
    r = _eval_with_strong_corpus()
    assert r.verdict == "PROMOTE"
    assert any("HOLDOUTS_NON_INDEPENDENT" in f for f in r.flags)


def test_r2_finding_8_stale_corpus_emits_stderr_warning(tmp_path, capsys):
    """R2-8: warn when corpus end-dates differ by >1 day."""
    base = datetime(2026, 5, 1)
    cand = _strong_corpus(pnl_per_trade=200, base=base)
    # Baseline ends 5 days earlier.
    base_old = _strong_corpus(pnl_per_trade=20, base=base - timedelta(days=5))
    evaluate(cand, base_old, regime_cutoff=None,
             balance_cents=1_000_000_000, n_resamples=100)
    captured = capsys.readouterr()
    assert "corpora end" in captured.err
    assert "days apart" in captured.err


# ── R3 regression tests ───────────────────────────────────────────────


def test_r3_finding_1_invalid_numeric_args_route_to_usage_error(tmp_path):
    """R3-1: invalid CLI numeric flags must route to EXIT_USAGE,
    not crash with EXIT_REJECT (1) which sweep harnesses would
    misread as a clean candidate rejection."""
    p = tmp_path / "c.jsonl"
    _write_corpus(p, _strong_corpus(pnl_per_trade=200))

    # argparse exits 2 on type errors. Acceptable distinction from
    # EXIT_REJECT=1 — the test checks the exit is NOT 1.
    for bad_args in (
        ["--n-resamples", "0"],
        ["--n-resamples", "-5"],
        ["--holdout-days", "0"],
        ["--stratified-fraction", "0.0"],
        ["--stratified-fraction", "1.0"],
        ["--dd-multiplier", "0.5"],
        ["--implausible-fill-threshold", "1.5"],
        ["--per-product-floor", "-1"],
    ):
        res = subprocess.run(
            [sys.executable, "-m", "research.eval",
             "--corpus", str(p), "--baseline", str(p),
             "--regime-cutoff", "none", "--quiet"]
            + bad_args,
            cwd=str(REPO_ROOT), capture_output=True, text=True,
        )
        assert res.returncode != 1, (bad_args, res.returncode, res.stderr)
        assert res.returncode != 0, (bad_args, res.returncode, res.stderr)


def test_r3_finding_1_bootstrap_delta_safe_on_zero_resamples():
    """Defense-in-depth: programmatic call with n_resamples=0
    returns (0, 0) instead of raising StatisticsError on empty fmean."""
    base = datetime(2026, 5, 1)
    cand = [_record(base + timedelta(hours=i), cf_pnl_cents=200)
            for i in range(10)]
    delta, sigma = bootstrap_delta(cand, cand, n_resamples=0, seed=42)
    assert delta == 0.0 and sigma == 0.0


def test_cli_default_regime_cutoff_is_registered_default(tmp_path):
    """Default cutoff is REGISTERED_DEFAULT_CUTOFF (no --regime-cutoff
    passed)."""
    base = REGISTERED_DEFAULT_CUTOFF + timedelta(hours=1)
    cand = [_record(base + timedelta(hours=i), cf_pnl_cents=200)
            for i in range(40)]
    base_rs = [_record(base + timedelta(hours=i), cf_pnl_cents=20)
               for i in range(40)]
    cand_path = tmp_path / "c.jsonl"
    base_path = tmp_path / "b.jsonl"
    _write_corpus(cand_path, cand)
    _write_corpus(base_path, base_rs)
    res = subprocess.run(
        [sys.executable, "-m", "research.eval",
         "--corpus", str(cand_path),
         "--baseline", str(base_path),
         "--balance-cents", "1000000000",
         "--n-resamples", "100",
         "--quiet"],
        cwd=str(REPO_ROOT), capture_output=True, text=True,
    )
    assert "WARNING" not in res.stderr
    assert res.returncode in (0, 1)
