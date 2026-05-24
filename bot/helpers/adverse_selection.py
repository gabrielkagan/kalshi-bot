"""Adverse-selection guards (B1, ClickUp 86ba1zdwm).

Two independent gates protecting the 15M scanner against catastrophic
loss classes diagnosed 2026-05-21 (see kb/decisions/b1-orderbook-prior-gate-plan.md):

  Gate A — orderbook-prior:        check_orderbook_prior_gate(...)
  Gate B — HYPE high-price buf:    check_hype_high_price_buf_gate(...)

Both functions are PURE would-block predicates: take decision-time state,
return either the `filter_stage` literal to log when the gate WOULD fire,
or None when the trade WOULD pass. They DO NOT check the *_GATE_ENABLED
kill-switch flags — that gating happens scanner-side so that shadow rows
log regardless of enable state (TM96 R-p7-deploy-r10 precedent for
counterfactual measurement during rollback).

The scanner's responsibility is:
  (a) call the would-block predicates,
  (b) log a shadow row when either returns non-None (always — even when
      the corresponding gate is disabled),
  (c) set _tm_intercepted=False / _dc_live_enabled=False only when the
      corresponding gate is ENABLED.

Constants live in bot/constants.py. The filter_stage literals are
registered in bot.helpers.cohort_attribution.COHORT_PARTITION_STAGES
per the cell-block discipline in bot/CLAUDE.md.
"""
from __future__ import annotations

import math
from typing import Any, Dict, Iterable, List, Optional, Tuple

from bot.constants import (
    HYPE_HIGH_PRICE_BUF_GATE_FILTER_STAGE,
    HYPE_HIGH_PRICE_BUF_GATE_MIN_BUF_PCT,
    HYPE_HIGH_PRICE_BUF_GATE_MIN_ENTRY_CENTS,
    ORDERBOOK_PRIOR_GATE_FILTER_STAGE,
    ORDERBOOK_PRIOR_GATE_MIN_CONVICTION_CENTS,
    ORDERBOOK_PRIOR_GATE_MIN_DISAGREE,
    ORDERBOOK_PRIOR_GATE_MIN_ENTRY_CENTS,
    ORDERBOOK_PRIOR_GATE_MIN_NO_BID_PRICE,
)


def _no_conviction_cents(
    yes_asks: Iterable[Tuple[int, int]],
    min_no_bid_price: int,
) -> int:
    """Sum (depth × no_bid_price) over NO bid levels at price >= min_no_bid_price.

    Kalshi orderbook stores yes_asks as YES sell-side directly; each (yes_ask_price,
    depth) tuple inverts to a NO bid at (100 - yes_ask_price) with the same depth.
    Below 2c the bids are mostly market-maker liquidity providers, not real NO
    conviction, so the threshold filters those out.
    """
    total = 0
    for level in yes_asks or ():
        try:
            yes_ask_price, depth = level[0], level[1]
        except (TypeError, IndexError, KeyError):
            continue
        if yes_ask_price is None or depth is None:
            continue
        no_bid_price = 100 - int(yes_ask_price)
        if no_bid_price >= min_no_bid_price:
            total += int(depth) * no_bid_price
    return total


def check_orderbook_prior_gate(
    calibrated_prob: float,
    no_ask_cents: int,
    yes_asks: Iterable[Tuple[int, int]],
    entry_price_cents: Optional[int] = None,
) -> Optional[str]:
    """Gate A — orderbook-prior adverse-selection would-block predicate.

    Returns ORDERBOOK_PRIOR_GATE_FILTER_STAGE when:
      - entry_price_cents >= ORDERBOOK_PRIOR_GATE_MIN_ENTRY_CENTS (90c),
      - bot's calibrated_prob exceeds the market's lower-bound estimate
        (1 - no_ask/100, derived from best_yes_bid) by strictly more than
        MIN_DISAGREE (0.05), AND
      - the NO bid book has strictly more than MIN_CONVICTION_CENTS ($5)
        of conviction at prices >= MIN_NO_BID_PRICE (2c, dropping pure
        market-maker liquidity at 0-1c).

    Returns None otherwise. Does NOT check the *_GATE_ENABLED flag —
    that's the scanner's responsibility (see module docstring).

    Args:
        calibrated_prob: bot's model probability for YES outcome (0.0-1.0).
        no_ask_cents: market's best NO ask price in cents.
        yes_asks: orderbook yes-asks levels as iterable of (price, depth)
            tuples. Kalshi inverts these to NO bids via (100 - price).
        entry_price_cents: candidate entry price in cents. Defaults to None
            (no entry-price gate) to preserve testability of the disagree+
            conviction logic in isolation; production callers MUST pass it.
            Enforced by AST contract
            `tests/contracts/test_b1_scanner_wiring.py::test_b1_call_sites_pass_entry_price_cents_to_gate_a`
            — fails RED if any scanner call site omits the kwarg.
    """
    if calibrated_prob is None or no_ask_cents is None:
        return None
    if entry_price_cents is not None and entry_price_cents < ORDERBOOK_PRIOR_GATE_MIN_ENTRY_CENTS:
        return None
    # market's lower-bound YES belief = (100 - no_ask) / 100 = best_yes_bid/100
    market_p_floor = (100 - no_ask_cents) / 100.0
    disagree = calibrated_prob - market_p_floor
    if disagree <= ORDERBOOK_PRIOR_GATE_MIN_DISAGREE:
        return None
    conv = _no_conviction_cents(yes_asks, ORDERBOOK_PRIOR_GATE_MIN_NO_BID_PRICE)
    if conv <= ORDERBOOK_PRIOR_GATE_MIN_CONVICTION_CENTS:
        return None
    return ORDERBOOK_PRIOR_GATE_FILTER_STAGE


def check_hype_high_price_buf_gate(
    asset: str,
    entry_price_cents: int,
    bot_buf_pct: float,
) -> Optional[str]:
    """Gate B — HYPE high-price buf measurement-noise would-block predicate.

    Returns HYPE_HIGH_PRICE_BUF_GATE_FILTER_STAGE when:
      asset == "HYPE" AND entry_price_cents >= MIN_ENTRY_CENTS (98)
                       AND bot_buf_pct < MIN_BUF_PCT (0.75, strict)

    Returns None otherwise. Does NOT check the *_GATE_ENABLED flag —
    that's the scanner's responsibility (see module docstring).

    HYPE-only because HYPE has the widest single-venue feed-divergence
    distribution (p99 = 76.6 bps vs BTC's 33.1 bps in the R0 sim).
    Other assets' divergence is tight enough that thin buf isn't
    structurally predictive of loss; per-asset gates for them are
    net-negative in the R0 sim.
    """
    if asset != "HYPE":
        return None
    if entry_price_cents is None or bot_buf_pct is None:
        return None
    if entry_price_cents < HYPE_HIGH_PRICE_BUF_GATE_MIN_ENTRY_CENTS:
        return None
    if bot_buf_pct >= HYPE_HIGH_PRICE_BUF_GATE_MIN_BUF_PCT:
        return None
    return HYPE_HIGH_PRICE_BUF_GATE_FILTER_STAGE


def _parse_book_side(entries: Iterable[Any]) -> Dict[int, int]:
    """Normalize a Kalshi WS book side (list of [price, qty]) to {price_cents: total_qty}.

    Mirrors OrderExecutor._extract_book_levels parsing semantics: float price < 1.0
    is treated as probability (x100->cents); float >= 1.0 already-cents. Drops bools,
    NaN/Inf, negative qty, out-of-range prices. Duplicate price levels are merged.
    """
    out: Dict[int, int] = {}
    for entry in entries or ():
        if isinstance(entry, (list, tuple)):
            if len(entry) < 2:
                continue
            price, qty = entry[0], entry[1]
        elif isinstance(entry, dict):
            if "quantity" not in entry:
                continue
            price = entry.get("price")
            qty = entry.get("quantity")
        else:
            continue
        if isinstance(price, bool) or isinstance(qty, bool):
            continue
        try:
            if isinstance(qty, float) and not math.isfinite(qty):
                continue
            qty_int = int(qty)
            if qty_int <= 0:
                continue
            if isinstance(price, float):
                if not math.isfinite(price):
                    continue
                price_cents = round(price * 100) if price < 1.0 else int(price)
            else:
                price_cents = int(price)
        except (TypeError, ValueError, OverflowError):
            continue
        if price_cents < 0 or price_cents > 100:
            continue
        out[price_cents] = out.get(price_cents, 0) + qty_int
    return out


def extract_no_ask_and_yes_asks(
    ob_data: Optional[Dict[str, Any]],
) -> Tuple[Optional[int], List[Tuple[int, int]]]:
    """Pull (no_ask_cents, yes_asks_list) from a live Kalshi WS orderbook dict.

    Schema: ob_data = {"yes": [[price, qty], ...], "no": [[price, qty], ...]}
    where both sides are BID books (people willing to buy YES / buy NO).

    Derives:
      no_ask_cents = 100 - best_yes_bid    (selling NO == buying YES at 100-X)
      yes_asks     = [(100 - no_bid_p, q)] (selling YES == buying NO at 100-X)

    Returns (None, []) when ob_data is missing/malformed.
    """
    if not isinstance(ob_data, dict):
        return None, []
    yes_bid_map = _parse_book_side(ob_data.get("yes"))
    no_bid_map = _parse_book_side(ob_data.get("no"))
    best_yes_bid = max(yes_bid_map) if yes_bid_map else None
    no_ask = (100 - best_yes_bid) if best_yes_bid is not None else None
    # yes_asks = inverted no_bids; drop no_bid=100 (yes_ask=0 nonsense)
    yes_asks = [(100 - p, q) for p, q in no_bid_map.items() if p < 100]
    return no_ask, yes_asks


def check_15m_entry_gates(
    asset: str,
    entry_price_cents: int,
    calibrated_prob: float,
    bot_buf_pct: float,
    ob_data: Optional[Dict[str, Any]],
) -> Optional[str]:
    """Orchestrator — run both B1 would-block predicates against a 15M entry candidate.

    Returns the first firing gate's filter_stage, or None if both pass.
    Gate B (HYPE high-price buf) runs first because it has no orderbook
    dependency — cheap short-circuit on the common HYPE 98-99c case.

    NOTE: this orchestrator is for UNIT TESTING the combined behavior.
    Production scanner callers should invoke the two gate predicates
    SEPARATELY so each gate's kill-switch (*_GATE_ENABLED) gates trade-
    block independently while shadow logging fires unconditionally.
    """
    gate_b = check_hype_high_price_buf_gate(
        asset=asset,
        entry_price_cents=entry_price_cents,
        bot_buf_pct=bot_buf_pct,
    )
    if gate_b:
        return gate_b
    no_ask, yes_asks = extract_no_ask_and_yes_asks(ob_data)
    if no_ask is None:
        return None
    return check_orderbook_prior_gate(
        calibrated_prob=calibrated_prob,
        no_ask_cents=no_ask,
        yes_asks=yes_asks,
        entry_price_cents=entry_price_cents,
    )
