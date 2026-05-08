"""Bit 3.2: Cell-block predicate helpers, extracted from bot/_impl.py."""
from typing import Optional, Tuple

from bot.constants import *  # noqa: F401,F403 — HIGH_PRICE_STC_BLOCK_*, TM98_HIGHPRICE_BLEED_BLOCK_*, SOL_TAKER_LOWPRICE_BLEED_BLOCK_*, WEATHER_NO_EXCLUDED_CITY_PREFIXES

def should_block_high_price_stc_band(
    asset: Optional[str],
    side: Optional[str],
    entry_price_cents: Optional[int],
    seconds_to_close: Optional[float],
    enabled: Optional[bool] = None,
) -> bool:
    """Return True if this entry falls in the 96¢ × {SOL,XRP} × 2-5min STC danger CELL.

    PURE CELL PREDICATE — does NOT consider strategy. For the actual gate decision
    (which exempts profitable strategies inside the cell), use
    `should_block_high_price_stc_candidate()`.

    See kb/decisions/96c-sol-xrp-2to5min-block-2026-04-26.md for the data and rationale.
    """
    if enabled is None:
        enabled = HIGH_PRICE_STC_BLOCK_ENABLED
    if not enabled:
        return False
    if asset not in HIGH_PRICE_STC_BLOCK_ASSETS:
        return False
    if side != "yes":
        return False
    if entry_price_cents != HIGH_PRICE_STC_BLOCK_PRICE_CENTS:
        return False
    if seconds_to_close is None:
        return False
    if not (HIGH_PRICE_STC_BLOCK_STC_LO_S <= seconds_to_close <= HIGH_PRICE_STC_BLOCK_STC_HI_S):
        return False
    return True


def should_block_high_price_stc_candidate(
    asset: Optional[str],
    side: Optional[str],
    entry_price_cents: Optional[int],
    seconds_to_close: Optional[float],
    strategy: Optional[str],
    enabled: Optional[bool] = None,
) -> bool:
    """Return True if this candidate is in the danger cell AND uses a bleeder strategy.

    Strategy-aware composite of cell predicate + bleeder-strategy check. Wins
    inside the cell (TM-96, TM-untagged, TAKER_NOW, MAKER_AGGRESSIVE, decided_t1*,
    weekend_discount, PANIC_CAPTURE) pass through untouched.

    Caller invokes this on each `selected` candidate at end of scan() and drops
    matches before returning the candidate list.

    See kb/decisions/96c-sol-xrp-2to5min-block-2026-04-26.md.
    """
    if not should_block_high_price_stc_band(
            asset, side, entry_price_cents, seconds_to_close, enabled=enabled):
        return False
    if strategy is None:
        return False
    return strategy in HIGH_PRICE_STC_BLOCK_BLEEDER_STRATEGIES


def should_block_tm98_highprice_bleed_candidate(
    asset: Optional[str],
    side: Optional[str],
    entry_price_cents: Optional[int],
    seconds_to_close: Optional[float],
    strategy: Optional[str],
    enabled: Optional[bool] = None,
) -> bool:
    """Return True iff candidate is in {BTC,ETH,XRP} × TM98 × 97-98¢ × 121-300s.

    Strategy-aware: only fires for terminal_momentum_98 (other strategies in
    the same price/STC cell aren't catastrophic).

    Default-OFF until operator flips TM98_HIGHPRICE_BLEED_BLOCK_ENABLED=1
    on the VPS .env.
    """
    if enabled is None:
        enabled = TM98_HIGHPRICE_BLEED_BLOCK_ENABLED
    if not enabled:
        return False
    if asset is None or asset not in TM98_HIGHPRICE_BLEED_BLOCK_ASSETS:
        return False
    if side != "yes":
        return False
    if entry_price_cents is None:
        return False
    if not (TM98_HIGHPRICE_BLEED_BLOCK_PRICE_LO <= entry_price_cents
            <= TM98_HIGHPRICE_BLEED_BLOCK_PRICE_HI):
        return False
    if seconds_to_close is None:
        return False
    if not (TM98_HIGHPRICE_BLEED_BLOCK_STC_LO_S <= seconds_to_close
            <= TM98_HIGHPRICE_BLEED_BLOCK_STC_HI_S):
        return False
    if strategy is None or strategy not in TM98_HIGHPRICE_BLEED_BLOCK_STRATEGIES:
        return False
    return True


def should_block_sol_taker_lowprice_bleed_candidate(
    asset: Optional[str],
    side: Optional[str],
    entry_price_cents: Optional[int],
    seconds_to_close: Optional[float],
    strategy: Optional[str],
    enabled: Optional[bool] = None,
) -> bool:
    """Return True iff candidate is SOL × TAKER_NOW × 85-89¢ × 121-300s STC.

    Default-OFF until operator flips SOL_TAKER_LOWPRICE_BLEED_BLOCK_ENABLED=1.
    """
    if enabled is None:
        enabled = SOL_TAKER_LOWPRICE_BLEED_BLOCK_ENABLED
    if not enabled:
        return False
    if asset is None or asset not in SOL_TAKER_LOWPRICE_BLEED_BLOCK_ASSETS:
        return False
    if side != "yes":
        return False
    if entry_price_cents is None:
        return False
    if not (SOL_TAKER_LOWPRICE_BLEED_BLOCK_PRICE_LO <= entry_price_cents
            <= SOL_TAKER_LOWPRICE_BLEED_BLOCK_PRICE_HI):
        return False
    if seconds_to_close is None:
        return False
    if not (SOL_TAKER_LOWPRICE_BLEED_BLOCK_STC_LO_S <= seconds_to_close
            <= SOL_TAKER_LOWPRICE_BLEED_BLOCK_STC_HI_S):
        return False
    if strategy is None or strategy not in SOL_TAKER_LOWPRICE_BLEED_BLOCK_STRATEGIES:
        return False
    return True


def should_exclude_weather_no_ticker(
    ticker: Optional[str],
    excluded_prefixes: Optional[frozenset] = None,
) -> bool:
    """Return True iff ticker belongs to an excluded weather-NO city family.

    Excluded cities (default: KXHIGHTLV / Las Vegas) bleed within the 39c+ live
    band even after the May-2 floor tightening. Predicate gates the live
    candidate creation in `_process_no_side_shadow`; shadow logging is
    unaffected so the data trail continues for forward-going analysis.

    Match is `ticker == prefix` or `ticker.startswith(prefix + "-")`. The
    trailing-dash anchor prevents collisions with hypothetical future Kalshi
    series that share a prefix substring (e.g. KXHIGHTLVENICE).
    """
    if excluded_prefixes is None:
        excluded_prefixes = WEATHER_NO_EXCLUDED_CITY_PREFIXES
    if not ticker:
        return False
    for _pfx in excluded_prefixes:
        if ticker == _pfx or ticker.startswith(_pfx + "-"):
            return True
    return False
