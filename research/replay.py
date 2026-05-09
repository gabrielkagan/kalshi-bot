"""Replay engine — Phase 3 scaffolding.

Mac-offline parallel-track per kb/decisions/autoresearch-design-may05.md and
kb/decisions/replay-engine-execution-plan-may09.md. NOT imported by bot/_impl.py.
The whole point is independence from the live decision path so this can validate it.

Each function is a clean port of a bounded piece of bot logic. The authoritative
source of truth for each is pinned in the docstring (function ref, not file:line —
file:line drifts with refactors).
"""
from __future__ import annotations

import math
from typing import Optional


# ── Fee formula ─────────────────────────────────────────────────────────────
# Authoritative source: models.py::calculate_fee
# Taker: ceil(fee_mult_taker × count × price × (100 − price) / 100)
# Maker: $0
# SPX gets 50% discount via fee_mult_taker=0.035; default is 0.07.

def replay_taker_fee(count: int, price_cents: int, fee_mult_taker: float = 0.07) -> int:
    return math.ceil(fee_mult_taker * count * price_cents * (100 - price_cents) / 100)


def replay_maker_fee(count: int, price_cents: int) -> int:
    return 0


# ── D-1: cf_pnl per-row identity ────────────────────────────────────────────
# Authoritative source: bot._impl::MainLoop::_poll_evaluated_opportunities
# (specifically the cf-computation block that produces would_have_profit).
#
# Inputs (from one evaluated_opportunities row):
#   entry_price       — row["market_price"] (int, cents) or None
#   market_result     — settled outcome string: "yes", "no", "all_yes", "all_no", or other
#   side              — row.get("side") or "yes"; either "yes" or "no"
#   count             — row.get("position_size") or 1
#   product_type      — row.get("product_type"); only matters for "weather" with low entry
#   weather_min_entry — WEATHER_MIN_ENTRY_PRICE; weather rows below this get cf_pnl=0
#
# Behavior:
#   1. entry_price is None → returns None ("unknown_no_price")
#   2. weather AND entry_price < weather_min_entry → returns 0 ("untradeable_price")
#   3. otherwise compute side-aware win/loss and apply taker pnl formula
#   4. unknown market_result → returns None
def replay_cf_pnl(
    *,
    entry_price: Optional[int],
    market_result: Optional[str],
    side: Optional[str] = "yes",
    position_size: Optional[int] = None,
    product_type: Optional[str] = None,
    weather_min_entry: int = 10,  # bot.constants.WEATHER_MIN_ENTRY_PRICE
    fee_mult_taker: float = 0.07,
) -> Optional[int]:
    """Replay the bot's counterfactual_pnl computation. Returns cents (int) or None.

    Byte-for-byte target: evaluated_opportunities.counterfactual_pnl. The live
    column stores `would_have_profit = pnl_taker` per the source-of-truth function.
    """
    count = position_size or 1

    if entry_price is None:
        return None

    if product_type == "weather" and entry_price < weather_min_entry:
        return 0

    side = (side or "yes").lower()
    result = (market_result or "").lower()

    if side == "no":
        is_win = result in ("no", "all_no")
        is_loss = result in ("yes", "all_yes")
    else:
        is_win = result in ("yes", "all_yes")
        is_loss = result in ("no", "all_no")

    if is_win:
        taker_fee = replay_taker_fee(count, int(entry_price), fee_mult_taker=fee_mult_taker)
        return (100 - int(entry_price)) * count - taker_fee
    if is_loss:
        taker_fee = replay_taker_fee(count, int(entry_price), fee_mult_taker=fee_mult_taker)
        return -(int(entry_price) * count + taker_fee)
    return None
