"""SyntheticRTIFeed — multi-venue CF-Benchmarks-shape synthetic RTI.

B2 (ClickUp 86ba1zf5j, plan kb/decisions/b2-synthetic-rti-feed-plan.md).
Approximates the CF Benchmarks Real Time Index that Kalshi 15M crypto
markets settle on (BRTI / ETHUSD_RTI / SOLUSD_RTI / XRPUSD_RTI /
DOGEUSD_RTI / BNBUSD_RTI / HYPEUSD_RTI), so the strike-buffer gate can
test against a multi-venue smoothed value instead of the bot's single-
venue Coinbase last-trade reading.

Methodology (faithful to `CME CF Real Time Indices Methodology` v16.6 §4
and `CF Spot Rate Methodology Guide` v15.1 §4 — identical algorithm,
per-asset parameters differ):

  1. Per constituent venue, take the L2 order book (bids desc, asks asc).
  2. Drop venues flagged potentially-erroneous: a venue whose mid-price
     deviates from the cross-venue median of mids by more than
     `potentially_erroneous_pct` is disregarded (CFB §5.3, single-pass).
  3. Consolidate surviving books into ONE order book (sizes aggregated by
     price), capping each level's size at the dynamic order-size cap
     (Eq 4-5: trimmed-mean of near-top sizes + 5 winsorized-σ).
  4. Build the mid price-volume curve at granularity `spacing`.
  5. Utilized depth v_T = largest contiguous volume (multiple of spacing)
     for which the mid-spread-volume curve stays ≤ `deviation_from_mid_pct`.
     If available depth < spacing, the index equals the consolidated-book
     mid (CFB §4.2 "Utilized Depth" note).
  6. RTI = exponential-density-weighted average of the mid PV curve over
     v ∈ {s, 2s, …, v_T}, weights ∝ e^(−λv), λ = 1/(0.3·v_T).

KNOWN SIMPLIFICATIONS vs the published spec (measured by the 14d shadow
soak RMSE; see plan doc acceptance section):
  - Constituent set is CFB-constituent ∩ free-public-L2-WS-reachable
    (Coinbase/Kraken/Bitstamp/Gemini). CFB venues NOT on a free public WS
    (Bullish/Crypto.com/LMAX/itBit) are absent — a documented coverage gap
    (e.g. 4-of-8 for BTC).
  - Potentially-erroneous reinstatement (CFB §5.3 rule 4 hysteresis) is
    single-pass: a venue is dropped for the current tick only; no
    sticky-disregard state across ticks.

SCOPE (B2a — bronze-first validation): this module is the PURE
aggregator only — `compute_synthetic_rti(books, ...)`. It is venue-
source-agnostic: the B2a offline RMSE harness feeds it books
reconstructed from bronze JSONL; the deferred B2b live path will feed it
books from in-bot WS venue feeds. The live-feed wrapper class
(`SyntheticRTIFeed` — staleness-drop + cache around this function) is
DEFERRED to the gated B2b ticket and is NOT built here, since its only
consumer (a live `VenueL2Feed`) does not exist until B2b and may never
if B2a's RMSE validation fails.

Imports: stdlib + numpy ONLY (no torch — to be pinned by an import-linter
contract when a bot-package consumer lands in B2b).
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

import numpy as np

Level = Tuple[float, float]  # (price, size)
Book = Tuple[List[Level], List[Level]]  # (bids desc, asks asc)


def _venue_mid(bids: List[Level], asks: List[Level]) -> Optional[float]:
    """Best-bid/best-ask mid for one venue, or None if a side is empty
    (CFB §5.2.1 rule 2: a book with no bids or no asks is erroneous)."""
    if not bids or not asks:
        return None
    best_bid = max(p for p, _ in bids)
    best_ask = min(p for p, _ in asks)
    return (best_bid + best_ask) / 2.0


def _filter_erroneous(
    books: Dict[str, Book], potentially_erroneous_pct: float
) -> Dict[str, Book]:
    """Drop venues whose mid deviates from the cross-venue median of mids
    by more than `potentially_erroneous_pct` (CFB §5.3, single-pass)."""
    mids: Dict[str, float] = {}
    for venue, (bids, asks) in books.items():
        m = _venue_mid(bids, asks)
        if m is not None and m > 0:
            mids[venue] = m
    if not mids:
        return {}
    median = float(np.median(list(mids.values())))
    if median <= 0:
        return {}
    tol = potentially_erroneous_pct / 100.0
    return {
        venue: books[venue]
        for venue, m in mids.items()
        if abs(m - median) / median <= tol
    }


def _aggregate_by_price(
    levels_per_venue: List[List[Level]], descending: bool
) -> List[Level]:
    """Merge levels from all venues, summing sizes at identical prices,
    sorted by price (descending for bids, ascending for asks)."""
    agg: Dict[float, float] = {}
    for levels in levels_per_venue:
        for price, size in levels:
            if size <= 0:
                continue
            agg[price] = agg.get(price, 0.0) + size
    return sorted(agg.items(), key=lambda kv: kv[0], reverse=descending)


def _order_size_cap(
    cons_bids: List[Level], cons_asks: List[Level]
) -> float:
    """Dynamic order-size cap C_T (CFB Eq 4-5) from the uncapped
    consolidated book. Returns +inf when too few near-top samples to
    estimate (cap doesn't bind)."""
    if not cons_bids or not cons_asks:
        return float("inf")
    best_bid = cons_bids[0][0]
    best_ask = cons_asks[0][0]
    # Eq 4a/4b: near-top sizes (within 5% of best), capped at 50 levels.
    ask_sizes = [s for p, s in cons_asks if p <= 1.05 * best_ask][:50]
    bid_sizes = [s for p, s in cons_bids if p >= 0.95 * best_bid][:50]
    sample = sorted(ask_sizes + bid_sizes)  # Eq 4c: ascending
    n = len(sample)
    if n < 2:
        return float("inf")
    arr = np.array(sample, dtype=float)
    k = int(np.floor(0.01 * n))  # Eq 4d
    # Eq 4e: trimmed mean over [k, n-k)
    trimmed = arr[k : n - k] if n - 2 * k > 0 else arr
    s_bar = float(np.mean(trimmed))
    # Eq 4f: winsorize first/last k to the boundary values
    wins = arr.copy()
    if k > 0:
        wins[:k] = arr[k]
        wins[n - k :] = arr[n - k - 1]
    # Eq 4h: sample std (ddof=1) of the winsorized set
    sigma = float(np.std(wins, ddof=1))
    return s_bar + 5.0 * sigma  # Eq 5


def _apply_cap(levels: List[Level], cap: float) -> List[Level]:
    return [(p, min(s, cap)) for p, s in levels]


def _marginal_price(fill_levels: List[Level], volume: float) -> Optional[float]:
    """Marginal price to fill `volume` walking `fill_levels` in fill order
    (bids desc for a sell, asks asc for a buy). None if depth insufficient."""
    cum = 0.0
    for price, size in fill_levels:
        cum += size
        if cum >= volume:
            return price
    return None


def compute_synthetic_rti(
    books: Dict[str, Book],
    spacing: float,
    deviation_from_mid_pct: float,
    potentially_erroneous_pct: float,
) -> Optional[float]:
    """CF-Benchmarks-shape Real Time Index from per-venue L2 books.

    See module docstring for the algorithm. Returns None when no venue
    survives the potentially-erroneous filter (CFB calculation failure).
    """
    surviving = _filter_erroneous(books, potentially_erroneous_pct)
    if not surviving:
        return None

    cons_bids = _aggregate_by_price(
        [b for b, _ in surviving.values()], descending=True
    )
    cons_asks = _aggregate_by_price(
        [a for _, a in surviving.values()], descending=False
    )
    if not cons_bids or not cons_asks:
        return None

    cap = _order_size_cap(cons_bids, cons_asks)
    cons_bids = _apply_cap(cons_bids, cap)
    cons_asks = _apply_cap(cons_asks, cap)

    best_bid = cons_bids[0][0]
    best_ask = cons_asks[0][0]
    consolidated_mid = (best_bid + best_ask) / 2.0

    total_bid = sum(s for _, s in cons_bids)
    total_ask = sum(s for _, s in cons_asks)
    max_depth = min(total_bid, total_ask)

    # CFB §4.2 "Utilized Depth": if reachable depth < spacing, the index
    # is the consolidated-book mid.
    if max_depth < spacing:
        return consolidated_mid

    # Sample the mid PV curve at v = s, 2s, ... up to reachable depth,
    # walking the contiguous-from-start region where mid-spread ≤ D.
    dev_tol = deviation_from_mid_pct / 100.0
    vs: List[float] = []
    mid_pv: List[float] = []
    v = spacing
    while v <= max_depth:
        ask_pv = _marginal_price(cons_asks, v)
        bid_pv = _marginal_price(cons_bids, v)
        if ask_pv is None or bid_pv is None:
            break
        m = (ask_pv + bid_pv) / 2.0
        mid_sv = ask_pv / m - 1.0  # CFB Eq 1f
        if mid_sv > dev_tol:
            break  # left the within-D contiguous region
        vs.append(v)
        mid_pv.append(m)
        v += spacing

    if not vs:
        # Even the first sample exceeded D — utilized depth clamps to
        # spacing, index = consolidated mid.
        return consolidated_mid

    v_t = vs[-1]
    lam = 1.0 / (0.3 * v_t)
    weights = np.exp(-lam * np.array(vs))
    wsum = float(np.sum(weights))
    if wsum <= 0:
        return consolidated_mid
    return float(np.dot(weights, np.array(mid_pv)) / wsum)
