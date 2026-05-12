"""Kelly sizing + drawdown_scaler extracted for sim PnL replay (R-p6-1#C10).

Per `kb-research/bot/p2-phase6-validation.md`: bot.py's `PositionSizer` is
sacred and may not be importable from research scripts (side effects, signal
handlers, etc.). Phase 6 SHIP PRECONDITION (lands in same commit as Phase 7
deploy): bot.py adds a startup-time parity-assert that its inline sizing
logic matches `sizing.compute_size(...)` on a fixed test vector.

This file mirrors the logic in `bot/config.py` (SIZING_TIERS, drawdown
thresholds, MAX_RISK_PER_TRADE), `bot.py` STC_SIZING_SCALER, and per-asset
ASSET_MAX_RISK_PER_TRADE caps from bot.py:226-229.
Doc-drift rule: if bot/config.py / bot/__main__.py change, update here in same commit.

R-p6-impl-2#C1 / R-p6-impl-4#C3: SIZING_TIERS as fraction-tuples, drawdown
0.85/0.75/0.65 ladder, MAX_RISK_PER_TRADE=0.25 cap, STC_SIZING_SCALER
(300/stc when stc>300), DRAWDOWN_HALT_FLOOR=0.10, per-asset risk caps.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


# bot/config.py:172-181 (post-Bit-12.1) — units are FRACTIONS (e.g., 0.04 = 4% edge).
SIZING_TIERS = [
    (0.04,   0.25),
    (0.025,  0.20),
    (0.018,  0.15),
    (0.012,  0.10),
    (0.009,  0.07),
    (0.007,  0.05),
    (0.005,  0.03),
    (0.0025, 0.02),
]
SIZING_TIER_FLOORS = [f for f, _ in SIZING_TIERS]
SIZING_TIER_RISK_FRACTIONS = [r for _, r in SIZING_TIERS]

DRAWDOWN_HALF_THRESHOLD = 0.85
DRAWDOWN_QUARTER_THRESHOLD = 0.75
DRAWDOWN_HALT_THRESHOLD = 0.65
DRAWDOWN_HALT_FLOOR = 0.10
MAX_RISK_PER_TRADE = 0.25

# R-p6-impl-4#C3: per-asset risk caps mirrored from bot.py:226-229.
ASSET_MAX_RISK_PER_TRADE = {
    'BTC': 0.15,
    'ETH': 0.20,
    'SOL': 0.15,
    'XRP': 0.15,
}

STC_SIZING_SCALER_KNEE = 300
STC_SIZING_SCALER_ENABLED = True


# R-p7-deploy-r3: edge-schedule + discount + bleeder constants previously
# lived in sim_pnl.py. Moved here to decouple integration.parity_assert from
# the sim_pnl module (which imports torch + pandas — heavy chain that broke
# tests/contracts/test_db_signatures.py on local-only-no-pandas environments).
# sim_pnl.py re-exports for back-compat.

# MIN_EDGE_BY_PRICE — 6-tier FRACTION schedule per bot.py:1180-1187.
MIN_EDGE_BY_PRICE_SCHEDULE = [
    (97, 0.0100),   # 97-99¢: 1.00%
    (95, 0.0075),   # 95-96¢: 0.75%
    (93, 0.0050),   # 93-94¢: 0.50%
    (91, 0.0020),   # 91-92¢: 0.20%
    (89, 0.0025),   # 89-90¢: 0.25%
    (0,  0.0025),   # <89¢: 0.25%
]

# Weekend / overnight discount thresholds (bot.py:853-864).
WEEKEND_EDGE_DISCOUNT = 0.60
WEEKEND_EDGE_FLOOR = 0.0
OVERNIGHT_EDGE_DISCOUNT = 0.60

# bot.py:1213-1226 — strategies that get blocked at high price + low STC.
HIGH_PRICE_STC_BLOCK_BLEEDER_STRATEGIES = frozenset({
    'decided_t2', 'decided_t2_z2', 'decided_t2_z25', 'MAKER_PATIENT',
})

# bot.py:238-244 — STC_EXTENDED zone per-asset floors.
STC_EXTENDED_BUFFER_RESCUE = 0.25
STC_EXTENDED_PER_ASSET_FLOOR = {
    'BTC': 93,
    'ETH': 90,
    'SOL': 95,
    'XRP': 92,
}


@dataclass
class SizingResult:
    contract_count: int
    risk_fraction: float
    tier_idx: int             # 0=highest tier, 7=lowest; -1 if no tier matched
    drawdown_scaler: float    # 1.0 = no scaling, 0.5 = half, etc.
    stc_scaler: float         # 1.0 = no scaling, 300/stc otherwise
    notional_cents: int


def lookup_tier(fee_adjusted_edge_frac: float) -> tuple[int, float]:
    """Given fee-adjusted edge as FRACTION (e.g., 0.015 = 1.5%), return
    (tier_idx, risk_fraction). tier_idx=-1 if edge below all tiers."""
    for i, (floor, risk) in enumerate(SIZING_TIERS):
        if fee_adjusted_edge_frac >= floor:
            return (i, risk)
    return (-1, 0.0)


def compute_drawdown_scaler(
    current_balance_cents: int,
    hwm_cents: int,
) -> float:
    """bot/config.py drawdown ladder: halve at 0.85, quarter at 0.75, halt floor
    at 0.65 (returns DRAWDOWN_HALT_FLOOR=0.10 — bot.py doesn't fully halt in
    sim; tiny floor preserves replay continuity)."""
    if hwm_cents <= 0:
        return 1.0
    ratio = current_balance_cents / hwm_cents
    if ratio < DRAWDOWN_HALT_THRESHOLD:
        return DRAWDOWN_HALT_FLOOR
    if ratio < DRAWDOWN_QUARTER_THRESHOLD:
        return 0.25
    if ratio < DRAWDOWN_HALF_THRESHOLD:
        return 0.50
    return 1.0


def compute_stc_scaler(seconds_to_close: float) -> float:
    """bot.py STC_SIZING_SCALER: contracts *= 300/stc when stc>300.

    R-p7-r4#M1: caller (compute_size) applies `max(1, int(...))` after
    multiplication, so STC scaling can NEVER reduce a 1-contract position
    to 0. This matches bot.py and the integration.py mirror — intentional
    parity. Sim PnL operators should know that "STC scaler" is bounded
    below by 1 contract."""
    if not STC_SIZING_SCALER_ENABLED:
        return 1.0
    if seconds_to_close <= STC_SIZING_SCALER_KNEE:
        return 1.0
    return STC_SIZING_SCALER_KNEE / float(seconds_to_close)


def compute_size(
    fee_adjusted_edge_frac: float,
    available_balance_cents: int,
    entry_price_cents: int,
    current_balance_cents: int,
    hwm_cents: int,
    seconds_to_close: float = 0.0,
    asset: Optional[str] = None,
) -> SizingResult:
    """Kelly tier × drawdown_scaler × stc_scaler → contract count, capped by
    MAX_RISK_PER_TRADE and per-asset ASSET_MAX_RISK_PER_TRADE.

    fee_adjusted_edge_frac: per A28/A53 in FRACTIONS (e.g., 0.021 = 2.1%).
    asset: per R-p6-impl-4#C3, applies per-asset cap (bot.py:13390+).
    Mirrors bot.py's PositionSizer.compute() + per-asset risk caps.
    """
    tier_idx, risk_fraction = lookup_tier(fee_adjusted_edge_frac)
    if tier_idx < 0:
        return SizingResult(0, 0.0, -1, 1.0, 1.0, 0)
    drawdown = compute_drawdown_scaler(current_balance_cents, hwm_cents)
    asset_cap = ASSET_MAX_RISK_PER_TRADE.get(asset, MAX_RISK_PER_TRADE)
    effective_risk = min(risk_fraction * drawdown, MAX_RISK_PER_TRADE, asset_cap)
    risk_cents = int(available_balance_cents * effective_risk)
    notional_cents = max(1, risk_cents)
    contract_count = max(0, notional_cents // max(1, entry_price_cents))
    stc_scaler = compute_stc_scaler(seconds_to_close)
    if contract_count > 0 and stc_scaler < 1.0:
        contract_count = max(1, int(contract_count * stc_scaler))
    return SizingResult(
        contract_count=int(contract_count),
        risk_fraction=float(risk_fraction),
        tier_idx=int(tier_idx),
        drawdown_scaler=float(drawdown),
        stc_scaler=float(stc_scaler),
        notional_cents=int(contract_count * entry_price_cents),
    )
