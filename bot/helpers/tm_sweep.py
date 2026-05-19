"""Bit 3.2: Terminal-momentum sweep helpers, extracted from bot/_impl.py."""
import math
from typing import Dict, List, Optional, Tuple

from bot.constants import *  # noqa: F401,F403 — TM_*, MIN_/MAX_ENTRY_PRICE, etc.

# Sprint 10.5b (2026-05-11): models relocated to bot/models.py. Can't top-level
# `from bot.models import calculate_taker_fee` here — would violate the
# `helpers-leaf` .importlinter contract (helpers can't import siblings).
# Lazy-import inside the consumer function (mirrors Sprint 10.5a
# bot/helpers/breakers.py → bot.infra.circuit_breaker pattern). `sys.modules`
# caches; per-call overhead negligible.


def _get_calculate_taker_fee():
    """Lazy-bind calculate_taker_fee from bot.models (Sprint 10.5b 2026-05-11)."""
    from bot.models import calculate_taker_fee
    return calculate_taker_fee


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

    calculate_taker_fee = _get_calculate_taker_fee()  # Sprint 10.5b lazy bind
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


def _resolve_buf_multiplier(buf_pct: Optional[float]) -> float:
    """Look up the wide-buffer size multiplier (Sim B, ticket 86ba0v6z1).

    TM_BUFFER_SIZE_MULTIPLIER is sorted ascending by floor; we walk from the
    last entry backward and return the multiplier of the first entry whose
    floor ≤ buf_pct. buf_pct=None → 1.0 (legacy / unknown buffer).
    """
    if buf_pct is None:
        return 1.0
    for floor, mult in reversed(TM_BUFFER_SIZE_MULTIPLIER):
        if buf_pct >= floor:
            return mult
    return 1.0


def tm_compute_contracts(price_cents: int, seconds_to_close: float,
                         bankroll_cents: int = 100000,
                         asset: str = "",
                         buf_pct: Optional[float] = None,
                         risk_cap_price: Optional[int] = None) -> int:
    """Margin × STC × buf-multiplier sizing for terminal momentum.

    Formula: TM_BASE × (100 - price) × stc_multiplier × buf_multiplier
    Capped at per-asset risk limit (structural: TM respects same caps as main pipeline).
    Negative-EV tiers (95c) get minimum sizing until WR proves above breakeven.
    Thin-buffer cap (BACKSTOP): when buf_pct < TM_THIN_BUFFER_PCT, cap contracts to
    TM_THIN_BUFFER_CONTRACT_CAP to bound tail risk (Apr 1-23: all 8 catastrophic
    TM losses ≥100ct were at sub-0.20% buffer; one ETH loss @ 0.155% buffer = -$178).

    Pre-Sim-B data (cap motivation, 1,056 trades, Apr 1-23 2026, refines
    earlier n=278):
    - STC < 180s: safe-zone boost ×1.5
    - STC 180-240s: danger zone ×0.5
    - STC 240+: standard ×1.0
    - Per-asset risk cap via TM_ASSET_RISK_CAPS
    - buf_pct < 0.20%: cap to TM_THIN_BUFFER_CONTRACT_CAP (=50)
      (losses avg buf_pct 0.189% vs wins 0.240% — the earlier "buffer doesn't
       predict" finding held on Apr 1-7 n=278; fails on full April sample.)

    Sim B (2026-05-19, ticket 86ba0v6z1) added the wide-buffer multiplier
    (TM_BUFFER_SIZE_MULTIPLIER) from a separate 30d phantom-corrected window
    (settled through 2026-05-18, n=1,055):
    - buf<0.20%: 1.0× (thin; cap still binds)
    - 0.20-0.40%: 1.0× (already-profitable band; no change)
    - 0.40-0.80%: 2.0× ($+1.40/ct realized; under-sized)
    - ≥0.80%: 3.0× ($+1.67/ct realized; under-sized)
    The 50-ct thin-buffer cap, per-asset risk caps, and TM_MAX_CONTRACTS
    hard ceiling all still bound the multiplier's upside.

    Caveat: the +$108/30d counterfactual sim figure that motivates Sim B
    assumes fixed-outcome (win/loss doesn't change with size). Larger sizes
    at thin top-of-book may degrade fills — post-deploy soak must validate
    realized PnL/contract in the 0.40-0.80% band against the +1.40¢/ct
    sim prediction. See kb/decisions/tm-buf-multiplier-sizing-plan.md.

    risk_cap_price (adversary A6): when computing max_by_risk, callers may
    pass the WORST-CASE fill price (e.g. MAX_ENTRY_PRICE=99 when sweeping)
    so dollars-at-risk respects the actual capital deployed at the highest
    swept tier, not the scan-time entry price. Defaults to price_cents.

    buf_pct semantics (ticket 86ba0vpfd, 2026-05-19): None preserves the legacy
    "no buffer info — caller predates buf_pct kwarg" bypass of the thin-buffer
    cap; NaN is treated as a conservative-unknown numeric value and triggers
    the cap (IEEE-754 `NaN < 0.20` is False, which would otherwise silently
    bypass).
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

    buf_mult = _resolve_buf_multiplier(buf_pct)

    ct = int(TM_BASE_CONTRACTS * margin * stc_mult * buf_mult)

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
    # (BACKSTOP — preserved alongside the new buf_multiplier per Sim B).
    # NaN defense (ticket 86ba0vpfd, 2026-05-19): IEEE-754 `NaN < 0.20` is False,
    # which would silently bypass the cap on a NaN buf_pct. Treat NaN as a
    # conservative-unknown numeric value and apply the cap. `None` (legacy
    # "no buffer info — caller predates buf_pct kwarg") intentionally still
    # bypasses; only NaN is the new defense surface.
    if buf_pct is not None:
        if math.isnan(buf_pct) or buf_pct < TM_THIN_BUFFER_PCT:
            ct = min(ct, TM_THIN_BUFFER_CONTRACT_CAP)

    return max(TM_MIN_CONTRACTS, min(TM_MAX_CONTRACTS, ct))


# ── Sim C — TM half-Kelly cal_mlp shadow (ticket 86ba0v7fc, 2026-05-19) ────
# Shadow-only Kelly-on-cal_mlp sizing helpers. The return value is logged
# to evaluated_opportunities.tm_shadow_kelly_* columns; NEVER consumed by
# production sizing. See kb/decisions/tm-half-kelly-shadow-plan.md.


def tm_shadow_kelly_contracts_with_bound(
    price_cents: int,
    bankroll_cents: int,
    asset: str,
    cal_mlp_p_mean: Optional[float],
    raw_prob_fallback: Optional[float],
    kelly_fraction: float = TM_SHADOW_KELLY_FRACTION,
    abs_loss_bound_cents: int = TM_SHADOW_KELLY_ABS_LOSS_BOUND_CENTS,
) -> Tuple[Optional[int], str]:
    """Counterfactual Kelly contracts + which constraint bound the size.

    SHADOW-ONLY — return value is logged via insert_evaluated_opportunity
    kwargs (tm_shadow_kelly_ct, tm_shadow_kelly_bound_hit, ...). NEVER
    consumed by production sizing.

    Returns:
        (ct, bound_hit) tuple where:
          - ct is Optional[int]: the counterfactual contract count, or
            None when no probability signal is available (null_prob).
          - bound_hit is one of:
              'null_prob'    — both probs are None; no sizing possible
              'raw_fallback' — cal_mlp_p_mean is None, fell back to raw_prob
              'kelly'        — Kelly's natural ct is the binding constraint
              'abs_loss'     — abs_loss_bound_cents is the binding constraint
              'asset_cap'    — TM_ASSET_RISK_CAPS[asset] is the binding constraint

    Kelly formula for a YES-side bet at price p cents:
        edge_numerator = (P * 100) - p   (cents expected value above breakeven)
        f = (P * 100 - p) / (100 - p)
        stake_cents = fractional_kelly * bankroll_cents
        ct = stake_cents / p   (capital deployed per contract is p cents)

    Negative-edge returns 0 ct (no betting on losing trades).
    NULL-safe: cal_mlp+raw both None → (None, 'null_prob').
    """
    # Probability signal selection — cal_mlp_p_mean preferred; raw_prob fallback
    if cal_mlp_p_mean is not None:
        p = float(cal_mlp_p_mean)
        used_fallback = False
    elif raw_prob_fallback is not None:
        p = float(raw_prob_fallback)
        used_fallback = True
    else:
        return (None, "null_prob")

    # Clamp price/margin sanity
    price = int(price_cents)
    margin = 100 - price
    if margin <= 0 or price <= 0:
        # Degenerate; no Kelly bet possible. Return 0 ct with the source tag.
        return (0, "raw_fallback" if used_fallback else "kelly")

    # Kelly fraction (full Kelly, fractional). Negative edge → 0 ct.
    p_pct = p * 100.0  # convert prob (0..1) to cents
    numerator = p_pct - price
    if numerator <= 0:
        # Negative or zero edge — don't bet.
        return (0, "raw_fallback" if used_fallback else "kelly")
    f_full = numerator / margin
    f = max(0.0, float(kelly_fraction) * f_full)

    # Stake cents → contracts at the entry price
    stake_cents = f * float(bankroll_cents)
    kelly_ct = int(stake_cents / price) if price > 0 else 0

    # Constraints (compute each independently; min wins; which one wins = bound_hit)
    # Abs-loss bound: ct * price ≤ abs_loss_bound_cents
    abs_loss_ct = int(abs_loss_bound_cents / price) if price > 0 else 0

    # Per-asset risk cap (mirrors TM_ASSET_RISK_CAPS; default 0.15 for unknown asset)
    risk_frac = TM_ASSET_RISK_CAPS.get(asset, 0.15)
    if bankroll_cents > 0 and price > 0:
        asset_cap_ct = int(bankroll_cents * risk_frac / price)
    else:
        asset_cap_ct = 0

    # Pick the smallest binding constraint
    candidates = (
        (kelly_ct, "kelly"),
        (abs_loss_ct, "abs_loss"),
        (asset_cap_ct, "asset_cap"),
    )
    ct, bound = min(candidates, key=lambda t: t[0])

    # If we fell back to raw_prob, mark the bound_hit as 'raw_fallback' to
    # surface the provenance — the analysis script wants to know which rows
    # got their prob from the fallback path independent of which numeric cap
    # bound the size. (Plan doc: "marks `prob=raw_fallback`".)
    if used_fallback:
        bound = "raw_fallback"

    # Floor at zero (negative-edge already handled above; this is defense-in-depth)
    ct = max(0, ct)
    return (ct, bound)


def tm_shadow_kelly_contracts(
    price_cents: int,
    bankroll_cents: int,
    asset: str,
    cal_mlp_p_mean: Optional[float],
    raw_prob_fallback: Optional[float],
    kelly_fraction: float = TM_SHADOW_KELLY_FRACTION,
    abs_loss_bound_cents: int = TM_SHADOW_KELLY_ABS_LOSS_BOUND_CENTS,
) -> Optional[int]:
    """Shadow-only counterfactual Kelly size — NEVER consumed by production sizing.

    Thin wrapper around `tm_shadow_kelly_contracts_with_bound` that returns
    only the contract count (drops the bound-hit tag). Use the `_with_bound`
    variant when you need both values for logging — callers in
    `bot.scanner` use that form to populate the 4 tm_shadow_kelly_*
    evaluated_opportunities columns in lockstep.

    Returns None when probability signal is unavailable (NULL cal_mlp + NULL
    raw_prob). Mirrors per-asset risk caps so the logged number is
    decision-realistic.
    """
    ct, _bound = tm_shadow_kelly_contracts_with_bound(
        price_cents=price_cents,
        bankroll_cents=bankroll_cents,
        asset=asset,
        cal_mlp_p_mean=cal_mlp_p_mean,
        raw_prob_fallback=raw_prob_fallback,
        kelly_fraction=kelly_fraction,
        abs_loss_bound_cents=abs_loss_bound_cents,
    )
    return ct
