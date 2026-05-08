"""Bit 3.2: Sizing helpers, extracted from bot/_impl.py."""
from bot.constants import *  # noqa: F401,F403 — BUFFER_SIZING_*, MIN_EDGE_BY_PRICE

def buffer_sizing_multiplier(spot_buffer_pct: float) -> float:
    """Return a sizing multiplier based on spot buffer at entry.

    Only called when BUFFER_SIZING_ENABLED = True.
    Thresholds from PPO analysis (Apr 7, n=63, 2 losses — preliminary):
    - Fat buffer (>= 0.20%): ×1.25 (high confidence, lean in)
    - Normal (0.10-0.20%): ×1.0 (baseline)
    - Thin (0.05-0.10%): ×0.5 (reduce exposure)
    - Critical (< 0.05%): ×0.25 (minimum — spot barely above threshold)
    """
    if spot_buffer_pct >= BUFFER_SIZING_FAT:
        return 1.25
    elif spot_buffer_pct >= BUFFER_SIZING_NORMAL:
        return 1.0
    elif spot_buffer_pct >= BUFFER_SIZING_CRITICAL:
        return 0.5
    else:
        return 0.25


def get_min_edge(entry_price_cents: int) -> float:
    """Return minimum fee-adjusted edge for a given entry price."""
    for price_floor, min_edge in MIN_EDGE_BY_PRICE:
        if entry_price_cents >= price_floor:
            return min_edge
    return 0.005
