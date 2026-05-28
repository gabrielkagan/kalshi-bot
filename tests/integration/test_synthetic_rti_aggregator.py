"""Tests for the B2 multi-venue synthetic RTI aggregator core.

[86ba1zf5j] bot/feeds/synthetic_rti.py — CF Benchmarks-shape consolidated
order-book Real Time Index approximation. Plan:
kb/decisions/b2-synthetic-rti-feed-plan.md.

The pure-function core is `compute_synthetic_rti(books, spacing,
deviation_from_mid_pct, potentially_erroneous_pct)`:

  - `books`: venue_name -> (bids, asks); each side a list of (price, size)
    tuples (bids descending by price, asks ascending).
  - Merge all (non-dropped) venue books into ONE consolidated order book.
  - Build a mid price-volume curve at granularity `spacing`; find the
    utilized depth v_T = max contiguous volume where the mid-spread-volume
    curve stays <= deviation_from_mid_pct; return the exponential-density-
    weighted sum of the mid PV curve up to v_T (lambda = 1/(0.3*v_T),
    weights normalized to 1).
  - Venues whose individual mid deviates from the cross-venue median by
    more than potentially_erroneous_pct are DROPPED before consolidation
    (CFB §5.3 potentially-erroneous-data rule).
  - Returns None when no venue survives.

Guards against:
- Methodology drift: the degenerate (depth < spacing) case MUST collapse
  to the consolidated-book mid (CFB methodology §4.2 "Utilized Depth"
  note: "is then effectively equal to the mid-price of the consolidated
  order book").
- Outlier-handling drift: a venue >PErr% from the cross-venue median must
  be dropped, not blended in.
- Empty-input crash: no venues -> None, never a raise.

These are falsifiable hand-computed pins, NOT snapshot regen. They pin the
CFB contract without depending on the exact exponential-weighting
internals (chosen so the degenerate cases are exact).
"""
from __future__ import annotations

import pytest

from bot.feeds.synthetic_rti import compute_synthetic_rti


def test_degenerate_depth_below_spacing_returns_consolidated_mid():
    """Two venues, tight 1-level books, spacing huge so utilized depth
    clamps to spacing. Per CFB methodology, the index then equals the
    consolidated-book mid = (best_bid + best_ask) / 2 = 100.05."""
    books = {
        "coinbase": ([(100.0, 5.0)], [(100.1, 5.0)]),
        "kraken": ([(100.0, 5.0)], [(100.1, 5.0)]),
    }
    rti = compute_synthetic_rti(
        books,
        spacing=1000.0,
        deviation_from_mid_pct=1.0,
        potentially_erroneous_pct=10.0,
    )
    assert rti == pytest.approx(100.05, abs=1e-6)


def test_outlier_venue_dropped_before_consolidation():
    """Three venues, one with a mid ~50% above the other two. With
    potentially_erroneous_pct=10, the outlier is dropped; the index
    reflects only the two healthy venues -> consolidated mid 100.05."""
    books = {
        "coinbase": ([(100.0, 5.0)], [(100.1, 5.0)]),
        "kraken": ([(100.0, 5.0)], [(100.1, 5.0)]),
        "rogue": ([(150.0, 5.0)], [(150.1, 5.0)]),
    }
    rti = compute_synthetic_rti(
        books,
        spacing=1000.0,
        deviation_from_mid_pct=1.0,
        potentially_erroneous_pct=10.0,
    )
    assert rti == pytest.approx(100.05, abs=1e-6)


def test_empty_books_returns_none():
    """No venues -> None (never a raise)."""
    rti = compute_synthetic_rti(
        {},
        spacing=1000.0,
        deviation_from_mid_pct=1.0,
        potentially_erroneous_pct=10.0,
    )
    assert rti is None


def test_deep_flat_book_exercises_exponential_weighting():
    """Deep book (depth >> spacing) so the exponential-weighted-sum path
    runs, not the depth<spacing clamp. With a perfectly flat book (bid 99
    huge, ask 101 huge), the mid PV curve is constant at 100 at every
    sample point and the mid-spread-volume stays at exactly D=1%
    (101/100-1), so all points are within utilized depth and the weighted
    average collapses to 100.0 regardless of the exponential weights."""
    books = {
        "coinbase": ([(99.0, 1000.0)], [(101.0, 1000.0)]),
        "kraken": ([(99.0, 1000.0)], [(101.0, 1000.0)]),
    }
    rti = compute_synthetic_rti(
        books,
        spacing=1.0,                 # depth (2000) >> spacing -> exp-weight path
        deviation_from_mid_pct=1.0,  # midSV == 1.0% exactly -> within D
        potentially_erroneous_pct=10.0,
    )
    assert rti == pytest.approx(100.0, abs=1e-6)
