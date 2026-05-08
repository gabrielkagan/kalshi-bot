"""Bit 3.2: Terminal-momentum sweep helpers, extracted from bot/_impl.py."""
from typing import Dict, List, Optional, Tuple

from bot.constants import *  # noqa: F401,F403 — TM_*, MIN_/MAX_ENTRY_PRICE, etc.
from models import calculate_taker_fee

def tm_sweep_extract_depths(yes_asks, tiers=TM_SWEEP_CAPTURE_TIERS):
    """Given a list of [price_cents, qty] yes-ask pairs (post _extract_book_levels),
    return {tier: total_qty} for each requested tier. Missing tiers → 0.
    Duplicate tiers in input are summed (defensive)."""
    out = {t: 0 for t in tiers}
    if not yes_asks:
        return out
    tier_set = set(tiers)
    for entry in yes_asks:
        try:
            price, qty = int(entry[0]), int(entry[1])
        except (TypeError, ValueError, IndexError):
            continue
        if price in tier_set and qty > 0:
            out[price] += qty
    return out


def tm_sweep_counterfactual_pnl(unfilled, entry_tier, depths, market_result,
                                sweep_tiers=TM_SWEEP_COUNTERFACTUAL_TIERS):
    """Counterfactual sweep PnL: what would unfilled remainder have earned if
    we'd sequentially IOC'd into each sweep_tier strictly above entry_tier?

    Returns (total_pnl_cents, breakdown_legs).
    breakdown_legs = [{"tier": int, "ct": int, "payoff": int}, ...].

    Win:  payoff = ct * (100 - tier) - taker_fee(ct, tier)
    Loss: payoff = -(ct * tier + taker_fee(ct, tier))
    Unrecognized market_result → (0, [])."""
    if market_result in ("yes", "all_yes"):
        is_win = True
    elif market_result in ("no", "all_no"):
        is_win = False
    else:
        return 0, []

    legs = []
    total = 0
    remaining = max(0, int(unfilled))
    for tier in sweep_tiers:
        if remaining <= 0:
            break
        if tier <= entry_tier:
            continue
        avail = int(depths.get(tier, 0) or 0)
        take = min(remaining, avail)
        if take <= 0:
            continue
        fee = calculate_taker_fee(take, tier)
        if is_win:
            payoff = take * (100 - tier) - fee
        else:
            payoff = -(take * tier + fee)
        legs.append({"tier": tier, "ct": take, "payoff": payoff})
        total += payoff
        remaining -= take
    return total, legs


def tm_compute_contracts(price_cents: int, seconds_to_close: float,
                         bankroll_cents: int = 100000,
                         asset: str = "",
                         buf_pct: Optional[float] = None,
                         risk_cap_price: Optional[int] = None) -> int:
    """Margin × STC-aware sizing for terminal momentum.

    Formula: TM_BASE × (100 - price) × stc_multiplier
    Capped at per-asset risk limit (structural: TM respects same caps as main pipeline).
    Negative-EV tiers (95c) get minimum sizing until WR proves above breakeven.
    Thin-buffer cap: when buf_pct < TM_THIN_BUFFER_PCT, cap contracts to
    TM_THIN_BUFFER_CONTRACT_CAP to bound tail risk (Apr 1-23: all 8 catastrophic
    TM losses ≥100ct were at sub-0.20% buffer; one ETH loss @ 0.155% buffer = -$178).

    Data (1,056 trades, Apr 1-23 2026, refines earlier n=278):
    - STC < 180s: safe-zone boost ×1.5
    - STC 180-240s: danger zone ×0.5
    - STC 240+: standard ×1.0
    - Per-asset risk cap via TM_ASSET_RISK_CAPS
    - buf_pct < 0.20%: cap to TM_THIN_BUFFER_CONTRACT_CAP (=50)
      (losses avg buf_pct 0.189% vs wins 0.240% — the earlier "buffer doesn't
       predict" finding held on Apr 1-7 n=278; fails on full April sample.)

    risk_cap_price (adversary A6): when computing max_by_risk, callers may
    pass the WORST-CASE fill price (e.g. MAX_ENTRY_PRICE=99 when sweeping)
    so dollars-at-risk respects the actual capital deployed at the highest
    swept tier, not the scan-time entry price. Defaults to price_cents.
    """
    margin = 100 - price_cents
    if margin <= 0:
        return TM_MIN_CONTRACTS

    # EV gate: negative-EV tiers get minimum sizing (collect data only)
    if price_cents in TM_NEGATIVE_EV_TIERS:
        return TM_MIN_CONTRACTS

    # STC multiplier
    if seconds_to_close < TM_STC_SAFE_THRESHOLD:
        stc_mult = TM_STC_SAFE_MULT
    elif seconds_to_close < TM_STC_DANGER_HI:
        stc_mult = TM_STC_DANGER_MULT
    else:
        stc_mult = TM_STC_NORMAL_MULT

    ct = int(TM_BASE_CONTRACTS * margin * stc_mult)

    # Per-asset risk cap (structural: TM no longer bypasses asset caps)
    if bankroll_cents > 0:
        risk_frac = TM_ASSET_RISK_CAPS.get(asset, 0.15)
        # Use risk_cap_price if provided (sweep-aware sizing), else fall
        # back to scan-time price. max_by_risk denominator is the WORST-case
        # fill price so cents-at-risk respect the cap regardless of sweep.
        # Adversary R3 A1: explicit None check, not truthiness — a future
        # caller passing 0 should NOT silently fall back to price_cents.
        _risk_price = risk_cap_price if risk_cap_price is not None else price_cents
        max_by_risk = int(bankroll_cents * risk_frac / _risk_price)
        ct = min(ct, max_by_risk)

    # Thin-buffer cap: bounds the fat tail when spot is close to threshold
    if buf_pct is not None and buf_pct < TM_THIN_BUFFER_PCT:
        ct = min(ct, TM_THIN_BUFFER_CONTRACT_CAP)

    return max(TM_MIN_CONTRACTS, min(TM_MAX_CONTRACTS, ct))
