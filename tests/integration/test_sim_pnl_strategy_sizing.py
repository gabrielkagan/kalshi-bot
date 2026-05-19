"""TDD for sim_pnl H7 — strategy-specific sizing dispatch.

Per kb/findings/sim-pnl-live-ws-divergence-rca-may05.md H7:

    Pre-H7 sim_pnl flatly applied the standard Kelly/tier-based
    `compute_size` to every accepted candidate, regardless of `strategy`.
    Production has FOUR strategy-specific sizing paths that diverge from
    the Kelly ladder — without this fix, sim_pnl over-sizes terminal
    momentum + decided-contract candidates by 5-25× because they bypass
    Kelly entirely in production.

    Strategy-specific paths in bot/_impl.py (must be mirrored in sim_pnl):

      1. terminal_momentum_{96,98,99} — bot/_impl.py:1226 `tm_compute_contracts`:
         ct = TM_BASE_CONTRACTS(=100) × margin × stc_mult
         stc_mult ∈ {1.5 (<180s), 0.5 (180-240s), 1.0 (≥240s)}
         per-asset risk cap × MAX_ENTRY_PRICE worst-case denom
         (TM_SWEEP_LIVE_ENABLED=1 default)
         thin-buffer cap of 50 contracts when buf_pct < 0.20%
         min 25, max 500. kelly_f stored as 0.0.

      2. decided_t1 / _t1b / _t2 / _t2_z25 / _t2_z2 — bot/_impl.py:14402 fixed-%:
         risk = 10% (T2_Z25) or 20% (T2_Z2 / T1 / T1B / T2)
         SOL price-tiered override: [(97c, 5%), (95c, 10%)]
         pos = max(1, balance * risk / price)
         per-asset risk cap (BTC/SOL/XRP=15%) applied after.

      3. weekend_discount — bot/_impl.py:14056 standard Kelly + fallback:
         WEEKEND_FIXED_RISK=7% kicks in when Kelly produces 0 contracts.
         Drawdown scaler applied to fallback sizing too.

      4. overnight_discount — bot/_impl.py:14232 standard Kelly only:
         No fallback. Identical to standard compute_size.

Fix shape (H7 contract — what these tests pin):

    1. Module constants mirroring bot/_impl.py:
       - TM_BASE_CONTRACTS, TM_STC_SAFE_THRESHOLD, TM_STC_DANGER_HI,
         TM_STC_SAFE_MULT, TM_STC_DANGER_MULT, TM_STC_NORMAL_MULT,
         TM_MIN_CONTRACTS, TM_MAX_CONTRACTS, TM_THIN_BUFFER_PCT,
         TM_THIN_BUFFER_CONTRACT_CAP, TM_NEGATIVE_EV_TIERS,
         TM_ASSET_RISK_CAPS, TM_SWEEP_LIVE_RISK_DENOM_PRICE.
       - DECIDED_CONTRACT_T2_Z25_RISK, DECIDED_CONTRACT_T2_Z2_RISK,
         DECIDED_CONTRACT_RISK, SOL_DC_RISK_TIERS,
         DC_PER_ASSET_RISK_CAP, _DC_STRATEGY_TO_RISK.
       - WEEKEND_FIXED_RISK.
       - TM_LIVE_STRATEGIES, DC_LIVE_STRATEGIES.

    2. Helper `_strategy_size(strategy, ...)` dispatches to the per-strategy
       formula. For default / TAKER_NOW / MAKER_PATIENT / overnight_discount /
       and any unknown strategy, falls through to `compute_size`.

    3. `run_sim_pnl` SQL SELECT-list adds `spot_price` and `threshold` so
       the TM thin-buffer cap can be replicated.

    4. `_replay_one_path` calls `_strategy_size` instead of `compute_size`
       directly.

See bot/_impl.py:1075-1288 (TM constants + tm_compute_contracts) and
bot/_impl.py:14367-14617 (decided contract sizing) for the production
references.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "cal_mlp"))


@pytest.fixture(autouse=True)
def _require_deps():
    pytest.importorskip("pandas")
    pytest.importorskip("numpy")
    pytest.importorskip("torch")


# ── Constant locks ────────────────────────────────────────────────────


def test_tm_constants_match_bot_py():
    """TM constants must match bot/_impl.py:1075-1115 (drift-protection per
    CLAUDE.md "doc-drift" rule)."""
    import sim_pnl

    assert sim_pnl.TM_BASE_CONTRACTS == 100
    assert sim_pnl.TM_STC_SAFE_THRESHOLD == 180
    assert sim_pnl.TM_STC_DANGER_HI == 240
    assert sim_pnl.TM_STC_SAFE_MULT == 1.5
    assert sim_pnl.TM_STC_DANGER_MULT == 0.5
    assert sim_pnl.TM_STC_NORMAL_MULT == 1.0
    assert sim_pnl.TM_MIN_CONTRACTS == 25
    assert sim_pnl.TM_MAX_CONTRACTS == 500
    assert sim_pnl.TM_THIN_BUFFER_PCT == 0.20
    assert sim_pnl.TM_THIN_BUFFER_CONTRACT_CAP == 50
    assert sim_pnl.TM_ASSET_RISK_CAPS == {
        'BTC': 0.15, 'ETH': 0.20, 'SOL': 0.15, 'XRP': 0.15,
    }
    # MAX_ENTRY_PRICE = 99 (bot/_impl.py:245). TM_SWEEP_LIVE_ENABLED defaults
    # to "1" (bot/_impl.py:1153) so risk denom is the worst-case 99 sweep tier.
    assert sim_pnl.TM_SWEEP_LIVE_RISK_DENOM_PRICE == 99
    assert sim_pnl.TM_LIVE_STRATEGIES == frozenset({
        'terminal_momentum_96', 'terminal_momentum_98', 'terminal_momentum_99',
    })


def test_dc_constants_match_bot_py():
    """Decided-contract constants must match bot/_impl.py:1042-1048."""
    import sim_pnl

    assert sim_pnl.DECIDED_CONTRACT_T2_Z25_RISK == 0.10
    assert sim_pnl.DECIDED_CONTRACT_T2_Z2_RISK == 0.20
    assert sim_pnl.DECIDED_CONTRACT_RISK == 0.20
    # Order matters: SOL_DC_RISK_TIERS is iterated and matches the FIRST
    # tier whose price floor the entry meets. Must be high→low.
    assert tuple(sim_pnl.SOL_DC_RISK_TIERS) == ((97, 0.05), (95, 0.10))
    # Per-asset risk caps from bot/_impl.py:251-254 (BTC/SOL/XRP=15%; ETH=20%
    # but DC code at bot/_impl.py:14418-14424 only caps BTC/SOL/XRP — ETH
    # uses raw DC risk).
    assert sim_pnl.DC_PER_ASSET_RISK_CAP == {
        'BTC': 0.15, 'SOL': 0.15, 'XRP': 0.15,
    }
    # Strategy → risk mapping (bot/_impl.py:14402-14404 + 14586-14590).
    assert sim_pnl._DC_STRATEGY_TO_RISK == {
        'decided_t1': 0.20,
        'decided_t1b': 0.20,
        'decided_t2': 0.20,
        'decided_t2_z25': 0.10,
        'decided_t2_z2': 0.20,
    }
    assert sim_pnl.DC_LIVE_STRATEGIES == frozenset({
        'decided_t1', 'decided_t1b', 'decided_t2',
        'decided_t2_z25', 'decided_t2_z2',
    })


def test_weekend_fixed_risk_constant_matches_bot_py():
    """WEEKEND_FIXED_RISK must match bot/_impl.py:968 (= 0.07)."""
    import sim_pnl

    assert sim_pnl.WEEKEND_FIXED_RISK == 0.07


# ── _tm_size unit tests ───────────────────────────────────────────────


def test_tm_size_safe_zone_uses_1_5_multiplier():
    """STC<180s → safe-zone boost ×1.5. Plenty-of-balance scenario so
    margin × stc formula determines the result (not the per-asset cap)."""
    import sim_pnl

    ct = sim_pnl._tm_size(
        price_cents=98, stc=120.0, balance_cents=10_000_000,  # $100k
        asset='BTC', buf_pct=1.0,
    )
    # margin=2, stc_mult=1.5 → 100 * 2 * 1.5 = 300
    # max_by_risk = 10_000_000 * 0.15 / 99 = 15,151 (won't bind)
    # Result = 300
    assert ct == 300


def test_tm_size_danger_zone_uses_0_5_multiplier():
    """STC 180-240s → danger ×0.5 multiplier."""
    import sim_pnl

    ct = sim_pnl._tm_size(
        price_cents=98, stc=200.0, balance_cents=10_000_000,
        asset='BTC', buf_pct=1.0,
    )
    # margin=2, stc_mult=0.5 → 100 * 2 * 0.5 = 100
    # 100 ≥ TM_MIN_CONTRACTS(25) → 100
    assert ct == 100


def test_tm_size_normal_zone_uses_1_0_multiplier():
    """STC≥240s → normal ×1.0."""
    import sim_pnl

    ct = sim_pnl._tm_size(
        price_cents=98, stc=260.0, balance_cents=10_000_000,
        asset='BTC', buf_pct=1.0,
    )
    # margin=2, stc_mult=1.0 → 100 * 2 * 1.0 = 200
    assert ct == 200


def test_tm_size_per_asset_risk_cap_binds_when_balance_low():
    """Per-asset cap = balance × risk_frac / TM_SWEEP_LIVE_RISK_DENOM_PRICE.
    Pin a scenario where the cap binds below the margin-formula output."""
    import sim_pnl

    # ETH @98c safe zone:
    # margin=2, stc_mult=1.5 → 300 unrestricted.
    # ETH risk_frac=0.20. Risk denom=99 (sweep-live).
    # balance=50,000c → max_by_risk = 50,000 * 0.20 / 99 = 101.
    ct = sim_pnl._tm_size(
        price_cents=98, stc=120.0, balance_cents=50_000,
        asset='ETH', buf_pct=1.0,
    )
    assert ct == 101


def test_tm_size_thin_buffer_caps_at_50():
    """buf_pct < TM_THIN_BUFFER_PCT(0.20%) → cap 50ct (overrides margin
    formula even when margin × stc_mult > 50)."""
    import sim_pnl

    ct = sim_pnl._tm_size(
        price_cents=98, stc=120.0, balance_cents=10_000_000,
        asset='BTC', buf_pct=0.10,  # < 0.20
    )
    # Pre-cap: 300. Post-thin-buffer: 50.
    assert ct == 50


def test_tm_size_thin_buffer_does_not_apply_when_buf_above_threshold():
    """buf_pct >= 0.20% → no cap."""
    import sim_pnl

    ct = sim_pnl._tm_size(
        price_cents=98, stc=120.0, balance_cents=10_000_000,
        asset='BTC', buf_pct=0.50,  # >= 0.20
    )
    assert ct == 300  # uncapped


def test_tm_size_buf_pct_none_passes_through():
    """buf_pct=None (caller has no spot/threshold info) → no thin-buffer
    cap. Mirrors bot/_impl.py: buf_pct=None defaults the cap off."""
    import sim_pnl

    ct = sim_pnl._tm_size(
        price_cents=98, stc=120.0, balance_cents=10_000_000,
        asset='BTC', buf_pct=None,
    )
    assert ct == 300


def test_tm_size_floors_at_25():
    """Output floored at TM_MIN_CONTRACTS=25."""
    import sim_pnl

    # Force a tiny output: extremely small balance; margin formula = 50
    # but max_by_risk = balance * 0.15 / 99 = (small).
    ct = sim_pnl._tm_size(
        price_cents=99, stc=200.0, balance_cents=1_000,  # $10
        asset='BTC', buf_pct=1.0,
    )
    # margin=1, stc_mult=0.5 → 50. max_by_risk = 1000*0.15/99 = 1. Pre-floor=1.
    # Floor 25.
    assert ct == 25


def test_tm_size_caps_at_500():
    """Output capped at TM_MAX_CONTRACTS=500. Use BTC@96c with huge balance."""
    import sim_pnl

    # margin=4, stc_mult=1.5 → 600.
    # BTC risk_frac=0.15. balance=10M, risk denom=99 → max_by_risk = ~15,151.
    # 600 ≤ 15,151 → 600. Cap to 500.
    ct = sim_pnl._tm_size(
        price_cents=96, stc=120.0, balance_cents=10_000_000,
        asset='BTC', buf_pct=1.0,
    )
    assert ct == 500


# ── _dc_size unit tests ───────────────────────────────────────────────


def test_dc_size_t2_z25_uses_10pct():
    """T2-Z25 → 10% fixed sizing. ETH (no asset-cap override)."""
    import sim_pnl

    # balance=10,000c ($100), price=95c, risk=0.10
    # pos = max(1, 10000 * 0.10 / 95) = max(1, 10) = 10
    pos = sim_pnl._dc_size('decided_t2_z25', price_cents=95,
                           balance_cents=10_000, asset='ETH')
    assert pos == 10


def test_dc_size_t2_z2_uses_20pct():
    """T2-Z2 → 20% fixed."""
    import sim_pnl

    # balance=10,000c, price=95c, risk=0.20
    # pos = 10000 * 0.20 / 95 = 21.05 → 21
    pos = sim_pnl._dc_size('decided_t2_z2', price_cents=95,
                           balance_cents=10_000, asset='ETH')
    assert pos == 21


def test_dc_size_t1_uses_default_20pct():
    """T1/T1B/T2 → DECIDED_CONTRACT_RISK=20% default."""
    import sim_pnl

    # ETH @95c, balance=$100, risk=0.20.
    # pos = 10000 * 0.20 / 95 = 21.
    pos = sim_pnl._dc_size('decided_t1', price_cents=95,
                           balance_cents=10_000, asset='ETH')
    assert pos == 21


def test_dc_size_sol_97c_uses_5pct_override():
    """SOL @97c+ → SOL_DC_RISK_TIERS override 5%."""
    import sim_pnl

    # SOL@97, balance=10,000c. risk=0.05 (override).
    # pos = 10000 * 0.05 / 97 = 5.15 → 5.
    # Per-asset cap (15%) = 10000 * 0.15 / 97 = 15. 5 < 15 → no cap binds.
    pos = sim_pnl._dc_size('decided_t1', price_cents=97,
                           balance_cents=10_000, asset='SOL')
    assert pos == 5


def test_dc_size_sol_95c_uses_10pct_override():
    """SOL @95c → SOL_DC_RISK_TIERS override 10%."""
    import sim_pnl

    # SOL@95, balance=10,000c. risk=0.10 (override).
    # pos = 10000 * 0.10 / 95 = 10.5 → 10.
    pos = sim_pnl._dc_size('decided_t1', price_cents=95,
                           balance_cents=10_000, asset='SOL')
    assert pos == 10


def test_dc_size_sol_94c_no_override_uses_default_20pct():
    """SOL <95c → no override; default 20% (then per-asset cap may bind)."""
    import sim_pnl

    # SOL@94, balance=10,000c. risk=0.20 (no override).
    # pos = 10000 * 0.20 / 94 = 21.27 → 21.
    # Per-asset cap (SOL=15%) = 10000 * 0.15 / 94 = 15. Cap binds → 15.
    pos = sim_pnl._dc_size('decided_t1', price_cents=94,
                           balance_cents=10_000, asset='SOL')
    assert pos == 15


def test_dc_size_btc_per_asset_cap_binds():
    """BTC default risk=20% but per-asset cap=15% binds."""
    import sim_pnl

    # BTC@93, balance=10,000c. risk=0.20.
    # pos pre-cap = 10000 * 0.20 / 93 = 21.
    # Cap = 10000 * 0.15 / 93 = 16.
    pos = sim_pnl._dc_size('decided_t1', price_cents=93,
                           balance_cents=10_000, asset='BTC')
    assert pos == 16


def test_dc_size_eth_no_per_asset_cap_applies():
    """ETH lives outside DC_PER_ASSET_RISK_CAP per bot/_impl.py:14418-14424
    (only BTC/SOL/XRP get the asset-cap branch). The structural lock
    is the constant: ETH absent from DC_PER_ASSET_RISK_CAP. A function-
    behavior assertion alone risks a coincidental match where the cap
    happens to equal the raw size (e.g. DC=20% == ETH cap=20%)."""
    import sim_pnl

    assert 'ETH' not in sim_pnl.DC_PER_ASSET_RISK_CAP, (
        "DC_PER_ASSET_RISK_CAP must NOT include ETH per bot/_impl.py:14418-14424 — "
        "ETH bypasses the per-asset cap branch in the DC sizing code."
    )
    # Behavior assertion: ETH gets 21ct (= int(10000 * 0.20 / 95)).
    pos = sim_pnl._dc_size('decided_t2_z2', price_cents=95,
                           balance_cents=10_000, asset='ETH')
    assert pos == 21


def test_dc_size_zero_balance_returns_zero():
    """balance<=0 → 0 (no division-by-zero, no negative position)."""
    import sim_pnl

    pos = sim_pnl._dc_size('decided_t1', price_cents=95,
                           balance_cents=0, asset='BTC')
    assert pos == 0


def test_dc_size_floors_at_one_contract():
    """`max(1, int(...))` per bot/_impl.py:14411 — DC always sizes at least 1
    when balance > 0."""
    import sim_pnl

    # Tiny balance, default 20% risk: 100c * 0.20 / 95 = 0.21 → int=0 → max(1, 0) = 1.
    pos = sim_pnl._dc_size('decided_t1', price_cents=95,
                           balance_cents=100, asset='ETH')
    assert pos == 1


# ── _strategy_size dispatch tests ────────────────────────────────────


def test_strategy_size_default_passes_through_to_compute_size():
    """Strategy=None / TAKER_NOW / MAKER_PATIENT / WAIT / etc. → identical
    output to compute_size. Covers ~80% of candidates (NULL strategy)."""
    import sim_pnl
    from sizing import compute_size

    kwargs = dict(
        fee_adjusted_edge_frac=0.025,  # tier 1 → 0.20
        available_balance_cents=100_000,
        entry_price_cents=92,
        current_balance_cents=100_000,
        hwm_cents=100_000,
        seconds_to_close=120.0,
        asset='BTC',
    )
    expected = compute_size(**kwargs)
    for strategy in (None, 'TAKER_NOW', 'MAKER_PATIENT', 'WAIT',
                     'MAKER_AGGRESSIVE', 'PANIC_CAPTURE'):
        got = sim_pnl._strategy_size(strategy=strategy, **kwargs)
        assert got.contract_count == expected.contract_count, (
            f"strategy={strategy!r} should fall through; "
            f"expected {expected.contract_count}, got {got.contract_count}"
        )
        assert got.tier_idx == expected.tier_idx, (
            f"strategy={strategy!r} tier mismatch"
        )


def test_strategy_size_terminal_momentum_uses_tm_formula():
    """TM strategy → tm_compute_contracts formula (NOT Kelly)."""
    import sim_pnl

    # TM-98 BTC safe zone, $100k balance. Standard Kelly at edge=2.5%
    # would give ~204 contracts on tier-1 (20% of $100k / 98c). TM
    # formula gives 300 (margin × 1.5 mult, uncapped).
    got = sim_pnl._strategy_size(
        strategy='terminal_momentum_98',
        fee_adjusted_edge_frac=0.025,
        available_balance_cents=10_000_000,
        entry_price_cents=98,
        current_balance_cents=10_000_000,
        hwm_cents=10_000_000,
        seconds_to_close=120.0,
        asset='BTC',
        spot_price=98_500.0, threshold=98_000.0,  # buf_pct ~0.51% (no cap)
    )
    assert got.contract_count == 300
    assert got.tier_idx == -1  # bypasses Kelly tier ladder


def test_strategy_size_terminal_momentum_buf_pct_thin_caps_at_50():
    """spot/threshold derive buf_pct = (s-t)/t*100. Thin buffer < 0.20% → cap 50."""
    import sim_pnl

    got = sim_pnl._strategy_size(
        strategy='terminal_momentum_98',
        fee_adjusted_edge_frac=0.025,
        available_balance_cents=10_000_000,
        entry_price_cents=98,
        current_balance_cents=10_000_000,
        hwm_cents=10_000_000,
        seconds_to_close=120.0,
        asset='BTC',
        # 98,100 vs 98,000 = 0.102% buf < 0.20% threshold
        spot_price=98_100.0, threshold=98_000.0,
    )
    assert got.contract_count == 50


def test_strategy_size_terminal_momentum_no_spot_threshold_defaults_buf_to_zero():
    """Without spot/threshold inputs, sim_pnl mirrors bot/_impl.py:13649 by
    defaulting buf_pct=0 (= 0% buffer), which fires the thin-buffer cap
    (50ct). Pre-fix sim_pnl used buf_pct=None which skipped the cap
    entirely → 6× over-sizing for missing-feature TM rows. R3 MINOR #3
    fix aligned the fallback with bot/_impl.py."""
    import sim_pnl

    got = sim_pnl._strategy_size(
        strategy='terminal_momentum_98',
        fee_adjusted_edge_frac=0.025,
        available_balance_cents=10_000_000,
        entry_price_cents=98,
        current_balance_cents=10_000_000,
        hwm_cents=10_000_000,
        seconds_to_close=120.0,
        asset='BTC',
        spot_price=None, threshold=None,
    )
    # buf_pct=0.0 → 0 < TM_THIN_BUFFER_PCT(0.20) → thin-buffer cap fires.
    assert got.contract_count == 50

    # Threshold present but 0 → divide-by-zero protection → buf_pct=0.0
    # → cap fires identically.
    got2 = sim_pnl._strategy_size(
        strategy='terminal_momentum_98',
        fee_adjusted_edge_frac=0.025,
        available_balance_cents=10_000_000,
        entry_price_cents=98,
        current_balance_cents=10_000_000,
        hwm_cents=10_000_000,
        seconds_to_close=120.0,
        asset='BTC',
        spot_price=98_000.0, threshold=0.0,
    )
    assert got2.contract_count == 50


def test_strategy_size_decided_contract_uses_fixed_pct():
    """DC strategies use fixed % per tier, NOT Kelly."""
    import sim_pnl

    # decided_t2_z25 @95c, $100 balance → 10 contracts (10% × $100 / 95c).
    got = sim_pnl._strategy_size(
        strategy='decided_t2_z25',
        fee_adjusted_edge_frac=0.0,      # would not pass any Kelly tier
        available_balance_cents=10_000,
        entry_price_cents=95,
        current_balance_cents=10_000,
        hwm_cents=10_000,
        seconds_to_close=120.0,
        asset='ETH',
    )
    assert got.contract_count == 10
    assert got.risk_fraction == 0.10
    assert got.tier_idx == -1


def test_strategy_size_weekend_discount_kelly_passes_when_nonzero():
    """When Kelly > 0, weekend_discount uses standard compute_size — no
    fallback. Behavior identical to default strategy at the same edge."""
    import sim_pnl
    from sizing import compute_size

    kwargs = dict(
        fee_adjusted_edge_frac=0.025,
        available_balance_cents=100_000,
        entry_price_cents=92,
        current_balance_cents=100_000,
        hwm_cents=100_000,
        seconds_to_close=120.0,
        asset='BTC',
    )
    expected = compute_size(**kwargs)
    got = sim_pnl._strategy_size(strategy='weekend_discount', **kwargs)
    assert got.contract_count == expected.contract_count
    assert got.contract_count > 0  # sanity — Kelly tier 1 should fire


def test_strategy_size_weekend_discount_falls_back_to_fixed_when_kelly_zero():
    """When Kelly=0 (edge below all tiers), weekend_discount uses
    WEEKEND_FIXED_RISK=7%. Drawdown scaler also applied to fallback."""
    import sim_pnl

    # Edge below all tiers (< 0.0025) → compute_size returns 0.
    # WEEKEND fallback: max(1, balance * 0.07 / price) = max(1, 100000 * 0.07 / 95) = 73.
    # Drawdown ratio = 100k/100k = 1.0 → no scaler.
    got = sim_pnl._strategy_size(
        strategy='weekend_discount',
        fee_adjusted_edge_frac=0.001,    # below tier 7 (0.0025)
        available_balance_cents=100_000,
        entry_price_cents=95,
        current_balance_cents=100_000,
        hwm_cents=100_000,
        seconds_to_close=120.0,
        asset='BTC',
    )
    assert got.contract_count == 73
    assert got.tier_idx == -1


def test_strategy_size_weekend_discount_fallback_applies_drawdown_scaler():
    """WEEKEND_FIXED_RISK fallback sizes scaled by drawdown ratio per
    bot/_impl.py:14077-14079."""
    import sim_pnl

    # Drawdown ratio = 50k/100k = 0.50 → DRAWDOWN_HALT_THRESHOLD(0.65) →
    # halt floor 0.10.
    # fixed_raw = max(1, 50000 * 0.07 / 95) = 36.
    # Scaled: max(1, int(36 * 0.10)) = 3.
    got = sim_pnl._strategy_size(
        strategy='weekend_discount',
        fee_adjusted_edge_frac=0.001,
        available_balance_cents=50_000,
        entry_price_cents=95,
        current_balance_cents=50_000,
        hwm_cents=100_000,             # in deep drawdown
        seconds_to_close=120.0,
        asset='BTC',
    )
    assert got.contract_count == 3


def test_strategy_size_weekend_discount_skips_fallback_on_negative_edge():
    """Regression: weekend_discount fallback must NOT fire when caller passes
    negative `fee_adjusted_edge_frac` to `_strategy_size`. Pre-86b9zk0aw the
    sim mirror lacked any Kelly-sign gate on the fallback predicate.

    This test exercises `_strategy_size` directly with a hand-crafted negative
    edge (the obvious-case raw-edge-negative path). The post-86b9zk3at
    structural wrap of `_replay_one_path`'s `edge_frac` through
    `_calibrated_edge_for_sizing` is exercised separately by
    `test_calibrated_edge_for_sizing_sol_91c_negative` +
    `test_sim_pnl_sizing_uses_calibrated_edge_helper`.

    Ticket 86b9zk0aw.
    """
    import sim_pnl

    got = sim_pnl._strategy_size(
        strategy='weekend_discount',
        fee_adjusted_edge_frac=-0.005,
        available_balance_cents=100_000,
        entry_price_cents=95,
        current_balance_cents=100_000,
        hwm_cents=100_000,
        seconds_to_close=120.0,
        asset='BTC',
    )
    assert got.contract_count == 0, (
        f"weekend_discount must NOT fire fallback on negative sim edge. "
        f"Got contract_count={got.contract_count}."
    )
    assert got.tier_idx == -1


def test_strategy_size_weekend_discount_fallback_boundary_at_zero_edge():
    """Boundary: fee_adjusted_edge_frac == 0.0 must NOT fire fallback.
    Mirrors production's `(_wknd_kelly_f or 0) > 0` gate which evaluates
    to False at Kelly==0. Pins boundary semantics in 86b9zk0aw."""
    import sim_pnl

    got = sim_pnl._strategy_size(
        strategy='weekend_discount',
        fee_adjusted_edge_frac=0.0,
        available_balance_cents=100_000,
        entry_price_cents=95,
        current_balance_cents=100_000,
        hwm_cents=100_000,
        seconds_to_close=120.0,
        asset='BTC',
    )
    assert got.contract_count == 0, (
        f"weekend_discount must NOT fire fallback at edge==0. "
        f"Got contract_count={got.contract_count}."
    )


def test_calibrated_edge_for_sizing_sol_91c_negative(monkeypatch):
    """Regression: deeper parity. The SOL 91c shape (raw p_mean positive,
    band-calibrated Kelly negative) must produce a NEGATIVE edge_frac
    through the new `_calibrated_edge_for_sizing` helper, which mirrors
    production's `calibrated_prob_for_sizing(asset, ...)` wrap at
    bot/scanner/__init__.py:3804. Pre-86b9zk3at, sim's edge_frac came
    from bare p_mean (POSITIVE for this row) and sim over-credited the
    weekend_discount fallback by claiming the bot would have sized.

    Per band_calibration baseline (SOL × 90-93 band): n=20, raw=0.800,
    band_prior=0.889952, k=30 → shrunk = 0.8540. At entry_price_cents=91,
    breakeven=0.91 with taker fee ~0.7c → fee_frac ~0.007 → calibrated
    edge_frac = 0.8540 - 0.91 - 0.007 = -0.063. Negative.

    Ticket 86b9zk3at. Kill switch (86b9znd21, 2026-05-19) disabled at
    runtime here so this regression continues to validate the
    underlying calibration math even though prod has the wholesale
    wrap short-circuited.
    """
    from bot.helpers import band_calibration as bc
    monkeypatch.setattr(bc, "BAND_CALIBRATION_KILL_SWITCH", False)
    import sim_pnl

    # SOL 91c, raw p_mean ~0.99 (high), band-calibrated → 0.8540
    p_mean = 0.9978
    asset = 'SOL'
    entry_price_cents = 91
    breakeven = 0.91
    fee_frac_taker = 0.007
    edge = sim_pnl._calibrated_edge_for_sizing(
        p_mean=p_mean,
        asset=asset,
        entry_price_cents=entry_price_cents,
        breakeven=breakeven,
        fee_frac_taker=fee_frac_taker,
        product_type='15m',
    )
    # Calibrated prob 0.8540, breakeven 0.91, fee 0.007 → edge = -0.063
    assert edge < 0, (
        f"Calibrated edge_frac for SOL 91c with raw p_mean=0.9978 must be "
        f"negative (band-calibration shrinks SOL × 90-93 to ~0.854 < breakeven "
        f"0.91). Got edge={edge}."
    )


def test_calibrated_edge_for_sizing_preserves_raw_for_non_15m():
    """Helper must short-circuit to raw p_mean for non-15M product types.
    Mirrors `calibrated_prob_for_sizing` contract: P4.1 is 15M-only."""
    import sim_pnl

    edge_15m = sim_pnl._calibrated_edge_for_sizing(
        p_mean=0.95, asset='BTC', entry_price_cents=99,
        breakeven=0.99, fee_frac_taker=0.007, product_type='15m',
    )
    edge_hourly = sim_pnl._calibrated_edge_for_sizing(
        p_mean=0.95, asset='BTC', entry_price_cents=99,
        breakeven=0.99, fee_frac_taker=0.007, product_type='hourly',
    )
    # hourly: helper returns raw_prob → edge = 0.95 - 0.99 - 0.007 = -0.047
    assert abs(edge_hourly - (0.95 - 0.99 - 0.007)) < 1e-9, (
        f"Non-15M product_type must short-circuit to raw p_mean. Got edge_hourly={edge_hourly}."
    )


def test_sim_pnl_sizing_uses_calibrated_edge_helper():
    """AST guard: the sizing edge_frac assignment in `simulate_replay` MUST
    route through `_calibrated_edge_for_sizing` (which wraps p_mean through
    `bot.helpers.band_calibration.calibrated_prob_for_sizing`), NOT use the
    bare `float(p_mean) - breakeven - fee_frac_taker` form.

    Pre-86b9zk3at, the sizing site used bare p_mean → sim's counterfactual
    over-credited weekend_discount on post-P4.1 rows. Post-fix, the site
    uses the helper → algebraic identity sign(kelly)===sign(edge) actually
    delivers parity with production.

    Pinning to the helper-call form rather than to a specific edge value
    keeps the test resilient to band-calibration baseline refreshes.
    Ticket 86b9zk3at.
    """
    import os
    sim_pnl_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        'scripts', 'cal_mlp', 'sim_pnl.py',
    )
    with open(sim_pnl_path) as f:
        source = f.read()
    # The sizing site historically read `edge_frac = float(p_mean) - breakeven - fee_frac_taker`.
    # Post-fix it routes through `_calibrated_edge_for_sizing(...)`.
    bare_pattern = "edge_frac = float(p_mean) - breakeven - fee_frac_taker"
    helper_pattern = "_calibrated_edge_for_sizing("
    assert bare_pattern not in source, (
        f"Sim sizing edge_frac must NOT use bare p_mean — pre-86b9zk3at form "
        f"`{bare_pattern}` found in scripts/cal_mlp/sim_pnl.py. Route through "
        f"_calibrated_edge_for_sizing(...) to mirror production's band-calibration wrap."
    )
    assert helper_pattern in source, (
        f"Sim sizing edge_frac must call `{helper_pattern}` to mirror production's "
        f"band-calibration sizing-prob wrap (bot/scanner/__init__.py:3804)."
    )


def test_strategy_size_overnight_discount_no_fallback():
    """overnight_discount has NO Kelly=0 fallback (bot/_impl.py:14232 only
    applies the standard compute_size — no equivalent of WEEKEND_FIXED_RISK)."""
    import sim_pnl

    # Edge below all tiers → compute_size = 0. overnight_discount must NOT fallback.
    got = sim_pnl._strategy_size(
        strategy='overnight_discount',
        fee_adjusted_edge_frac=0.001,
        available_balance_cents=100_000,
        entry_price_cents=95,
        current_balance_cents=100_000,
        hwm_cents=100_000,
        seconds_to_close=120.0,
        asset='BTC',
    )
    assert got.contract_count == 0


def test_lpne_constants_match_bot_py():
    """Round-4 adversarial MAJOR #1 — LPNE constants must match bot/_impl.py:1327-1334.
    LPNE bypasses Kelly entirely (flat fixed sizing); without the H7
    dispatcher mirror, sim_pnl Kelly-sizes LPNE rows at 3.5-5.9× the
    production size."""
    import sim_pnl

    assert sim_pnl.LPNE_FIXED_CONTRACTS == 50
    assert sim_pnl.LPNE_LIVE_STRATEGIES == frozenset({'low_price_near_expiry'})


def test_lpne_size_returns_fixed_50_when_balance_positive():
    """Mirror bot/_impl.py:13013 + 13057 — production stores position_size=50
    for every LPNE candidate row regardless of bankroll, edge, or STC."""
    import sim_pnl

    # Various balances, none should change the fixed 50.
    for bal in (10_000, 100_000, 1_000_000, 10_000_000):
        ct = sim_pnl._lpne_size(balance_cents=bal)
        assert ct == 50, f"LPNE balance={bal} → {ct} contracts (expected 50)"


def test_lpne_size_returns_zero_when_balance_zero():
    """Defensive: a zero/negative balance produces 0 contracts (no
    bot/_impl.py equivalent fires this path because LPNE intercepts at scan
    time when balance is non-zero, but the H7 dispatcher must handle
    edge cases gracefully)."""
    import sim_pnl

    assert sim_pnl._lpne_size(balance_cents=0) == 0
    assert sim_pnl._lpne_size(balance_cents=-100) == 0


def test_strategy_size_lpne_uses_fixed_50_not_kelly():
    """LPNE strategy → flat 50 contracts. With a high-edge row that
    would Kelly-tier into 25% (capped at BTC 15% asset cap = ~174ct
    on $100k), assert sim_pnl returns 50 instead. Standard Kelly path
    would 3.5× over-size LPNE rows."""
    import sim_pnl

    # BTC LPNE typical: price=85c, edge ~10%, STC=60s, $100k balance.
    got = sim_pnl._strategy_size(
        strategy='low_price_near_expiry',
        fee_adjusted_edge_frac=0.10,    # tier 0 (≥0.04 = 25% Kelly)
        available_balance_cents=10_000_000,  # $100k
        entry_price_cents=85,
        current_balance_cents=10_000_000,
        hwm_cents=10_000_000,
        seconds_to_close=60.0,
        asset='BTC',
    )
    assert got.contract_count == 50
    assert got.tier_idx == -1   # bypasses Kelly tier ladder


def test_replay_one_path_handles_null_available_balance_cents():
    """Round-3 MAJOR #1 regression — `int(np.nan or 100000)` crashes
    with ValueError because NaN is truthy in Python's bool semantics
    (`np.nan or 100000` returns `nan`, not 100000). The May 2-6 audit
    window has 0 NULL-balance rows but wider backtests have rows with
    NULL `available_balance_cents`. Without the NaN-safe fallback at
    sim_pnl.py:1242-ish, those wider backtests crash mid-replay."""
    import numpy as np
    import pandas as pd
    import sim_pnl

    # Build minimal one-row DF with NaN available_balance_cents.
    df = pd.DataFrame({
        'evaluation_time': pd.to_datetime(['2026-05-03T00:00:00Z']),
        'ticker': ['KXBTC15M-T'],
        'strategy': ['TAKER_NOW'],
        'side': ['yes'],
        'market_result': ['yes'],
        'p_pred': [0.96],
        'p_std': [0.01],
        'price_tier': [3],
        'stc_bucket': [2],
        'vol_regime_int': [0],
        'entry_price_cents': [92],
        'seconds_to_close': [120.0],
        'available_balance_cents': [np.nan],         # the failure trigger
        'fee_adjusted_edge': [0.025],
        'is_weekend': [0],
        'hour_of_day_utc': [12],
        'calibrated_prob': [0.96],
        'spot_price': [98_500.0],
        'threshold': [98_000.0],
    })

    # Stub conformal_artifact to avoid heavy machinery; predict_with_interval
    # is the actual gateway. Use a passthrough monkeypatch.
    class _DummyArtifact(dict):
        pass

    def _fake_predict_with_interval(p, p_std, *args, **kwargs):
        # Return p_mean=p, std, lo=p-0.05, hi=p+0.05
        return (p, p_std, p - 0.05, p + 0.05)

    import sim_pnl as _sp
    orig = _sp.predict_with_interval
    try:
        _sp.predict_with_interval = _fake_predict_with_interval
        # Should NOT raise ValueError on int(NaN).
        result = _sp._replay_one_path(
            df=df,
            conformal_artifact={},
            market_blend_w=0.0,
            asset='BTC',
            block_enabled=False,
            hwm_init_cents=100_000,
            start_balance_cents=100_000,
            gate_prob_source='stored_calibrated_prob',
        )
        # Any non-crashing return is sufficient for the regression.
        # (PnL math correctness is covered by other tests.)
        assert isinstance(result, dict)
        assert 'total_pessimistic_30d' in result
    finally:
        _sp.predict_with_interval = orig


def test_strategy_size_unknown_strategy_uses_compute_size():
    """An unknown strategy (not in TM/DC/discount sets) → compute_size."""
    import sim_pnl
    from sizing import compute_size

    kwargs = dict(
        fee_adjusted_edge_frac=0.025,
        available_balance_cents=100_000,
        entry_price_cents=92,
        current_balance_cents=100_000,
        hwm_cents=100_000,
        seconds_to_close=120.0,
        asset='BTC',
    )
    expected = compute_size(**kwargs)
    got = sim_pnl._strategy_size(strategy='hypothetical_new_strat', **kwargs)
    assert got.contract_count == expected.contract_count


# ── SQL SELECT-list AST guards ────────────────────────────────────────


SIM_PNL_PATH = REPO / "scripts" / "cal_mlp" / "sim_pnl.py"


def _sim_pnl_tree() -> ast.Module:
    return ast.parse(SIM_PNL_PATH.read_text())


def _find_func(name: str) -> ast.FunctionDef:
    for node in _sim_pnl_tree().body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"function {name!r} not found at module level")


def test_run_sim_pnl_select_includes_spot_price_and_threshold():
    """SQL SELECT-list must pull `spot_price` and `threshold` so the TM
    thin-buffer cap path can compute buf_pct = (spot - threshold) / threshold.
    Without these, sim_pnl can't replicate the 50ct cap that fires when
    buf_pct < 0.20%."""
    fn = _find_func('run_sim_pnl')
    select_literal = None
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        callee = None
        if isinstance(node.func, ast.Attribute):
            callee = node.func.attr
        elif isinstance(node.func, ast.Name):
            callee = node.func.id
        if callee != 'read_sql':
            continue
        if not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            select_literal = first.value
            break
    assert select_literal is not None, (
        "run_sim_pnl: no `read_sql(<str literal>, ...)` found"
    )
    upper = select_literal.upper()
    sel_start = upper.find('SELECT')
    from_start = upper.find('FROM')
    select_list = select_literal[sel_start:from_start]
    assert 'spot_price' in select_list, (
        f"H7 AST guard: run_sim_pnl SELECT-list must include `spot_price` "
        f"so TM thin-buffer cap path has the data. Got: {select_list!r}"
    )
    assert 'threshold' in select_list, (
        f"H7 AST guard: run_sim_pnl SELECT-list must include `threshold` "
        f"so TM thin-buffer cap path has the data. Got: {select_list!r}"
    )


def test_replay_one_path_calls_strategy_size_not_compute_size_directly():
    """AST guard: `_replay_one_path` must dispatch via `_strategy_size`
    (not call `compute_size` directly). Direct compute_size = silent
    H7 regression."""
    fn = _find_func('_replay_one_path')

    def _calls(name: str) -> bool:
        for node in ast.walk(fn):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name) and node.func.id == name:
                return True
        return False

    assert _calls('_strategy_size'), (
        "H7 AST guard: `_replay_one_path` must call `_strategy_size(...)` "
        "to dispatch per-strategy sizing. Without this call, sim_pnl "
        "applies standard Kelly to terminal_momentum + decided_contract "
        "candidates that bot/_impl.py sizes via fundamentally different "
        "formulas."
    )
    assert not _calls('compute_size'), (
        "H7 AST guard: `_replay_one_path` must NOT call `compute_size` "
        "directly — route through `_strategy_size` so per-strategy paths "
        "are honored. compute_size remains the default fall-through "
        "inside _strategy_size."
    )


# ── R5 MAJOR #1 — STC_EXTENDED buffer-rescue unit mismatch ────────────


def test_stc_extended_floor_passes_uses_buf_pct_not_edge_frac():
    """R5 MAJOR #1 — bot/_impl.py:16135 compares `_ext_buf` (= percent buffer
    `(spot - threshold) / threshold * 100`) against
    `STC_EXTENDED_BUFFER_RESCUE = 0.25` (i.e. 0.25% buffer). Pre-fix
    sim_pnl compared `edge_frac` (probability edge fraction, e.g.
    0.013) against the same 0.25 → always rejected realistic rows.
    Real-world impact: 18 BTC/SOL candidate rows in May 2-6 window
    that bot/_impl.py admitted via buffer rescue were silently rejected
    by sim_pnl (most wins, costing PnL accuracy)."""
    import sim_pnl

    # Setup: BTC sub-floor (90c < 93c BTC floor), STC=521s (300-600 zone).
    # Pre-fix: edge_frac=0.013 < 0.25 → reject. Now: buf_pct=0.260% >= 0.25% → admit.
    assert sim_pnl.stc_extended_floor_passes(
        asset='BTC',
        entry_price_cents=90,
        seconds_to_close=521.0,
        buf_pct=0.260,
    ) is True
    # Buffer just below threshold → reject.
    assert sim_pnl.stc_extended_floor_passes(
        asset='BTC',
        entry_price_cents=90,
        seconds_to_close=521.0,
        buf_pct=0.249,
    ) is False
    # buf_pct=None (no spot/threshold info) → defensive, reject sub-floor row.
    assert sim_pnl.stc_extended_floor_passes(
        asset='BTC',
        entry_price_cents=90,
        seconds_to_close=521.0,
        buf_pct=None,
    ) is False
    # Above per-asset floor → no rescue needed.
    assert sim_pnl.stc_extended_floor_passes(
        asset='BTC',
        entry_price_cents=95,
        seconds_to_close=521.0,
        buf_pct=0.0,  # buffer doesn't matter at 95c >= 93c floor
    ) is True


def test_stc_extended_floor_passes_buf_pct_units_match_bot_py():
    """Lock the units: `STC_EXTENDED_BUFFER_RESCUE` = 0.25 means 0.25%
    buffer (NOT 25% edge). bot/_impl.py:16131-16135 documents this as
    `buf>=0.25%`. A future maintainer changing the units in either
    file would silently break the rescue gate."""
    from sizing import STC_EXTENDED_BUFFER_RESCUE

    assert STC_EXTENDED_BUFFER_RESCUE == 0.25, (
        f"STC_EXTENDED_BUFFER_RESCUE={STC_EXTENDED_BUFFER_RESCUE}, "
        f"expected 0.25 (= 0.25% buffer threshold per bot/_impl.py:264)"
    )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
