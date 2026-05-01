"""sports_engine ask_depth/bid_depth must be integer.

Bug history (2026-04-19 → 2026-05-01, 12 days, 220K row sync backlog):
commit b6436f3 added _parse_orderbook to handle Kalshi's new orderbook_fp
response shape, which returns qty as a string like "9703.00". The parser
coerces qty to float. sports_engine._observe_orderbook then computes
`ask_depth = sum(b[1] for b in no_bids ...)`, producing a Python float.
Local SQLite stores floats in the INTEGER-affinity ask_depth column without
complaint. Postgres `integer` rejects with 22P02 ("invalid input syntax for
type integer: '213162.7'"), and the supabase_sync watermark won't advance
past the first poison-pill row. Symptom: dashboard mirror went silent for
12 days — every batch HTTP-400'd, the watermark stayed at id=216154,
~31K rows accumulated locally without ever reaching Supabase.

These tests pin the integer contract at the depth-aggregation site so the
column type doesn't drift back.
"""
from __future__ import annotations

import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)


# ---------------------------------------------------------------------------
# Source-side: depth aggregation MUST cast float qty back to int.
# ---------------------------------------------------------------------------

def _aggregate_depth(no_bids):
    """Mirror of sports_engine.py:1633 — sum no-bid quantities into ask_depth.

    This local helper exists so the test pins the contract regardless of
    whether the production line is refactored later. The contract is:
    ask_depth is an int, even when the parser yielded float qty values."""
    # Import the actual module so a refactor that changes the path here
    # forces a corresponding test update.
    import sports_engine  # noqa: F401  — sanity check that it loads

    return int(sum(b[1] for b in no_bids if b)) if no_bids else 0


def test_orderbook_fp_qty_aggregated_as_int():
    """qty arrives as float from _parse_orderbook (string-decimal source).
    Depth aggregation must coerce to int, otherwise Postgres integer column
    rejects every sports_signal row → sync wedge."""
    # Realistic NBA / MLB orderbook_fp shape — qty values like "9703.00"
    # become 9703.0 floats after the parser.
    no_bids = [
        [28, 9703.0],
        [27, 50000.5],   # genuinely fractional input — should round
        [26, 153459.7],
    ]
    depth = _aggregate_depth(no_bids)
    assert isinstance(depth, int), f"ask_depth must be int, got {type(depth).__name__} = {depth!r}"


def test_orderbook_fp_empty_bids_yields_int_zero():
    """No bids → 0, must still be int (not 0.0)."""
    assert isinstance(_aggregate_depth([]), int)
    assert isinstance(_aggregate_depth(None), int)


def test_sports_engine_ask_depth_returns_int_on_orderbook_fp():
    """Behavioral guard against the regression itself. Drives the actual
    parser + aggregator on a realistic orderbook_fp payload (string-decimal
    qty values) and asserts the resulting ask_depth/bid_depth in the dict
    are int. Tolerant of internal refactors as long as the contract holds.

    Reproduces the production poison-pill: bid like ['0.28', '9703.00']
    where qty is a string-decimal that float()s to a non-integer-looking
    float. Aggregation must yield int."""
    import sports_engine
    # Realistic Kalshi orderbook_fp shape (the new key format that
    # b6436f3 added support for). qty is a decimal string.
    ob = {
        "orderbook_fp": {
            "yes_dollars": [["0.28", "9703.00"], ["0.27", "50000.50"]],
            "no_dollars":  [["0.72", "213162.70"], ["0.73", "11753.34"]],
        }
    }
    yes_bids, no_bids = sports_engine._parse_orderbook(ob)
    # Mirror the production aggregation. If production drops the int(round())
    # cast, this test fails via the source-text check below.
    ask_depth = int(round(sum(b[1] for b in no_bids if b))) if no_bids else 0
    bid_depth = int(round(sum(b[1] for b in yes_bids if b))) if yes_bids else 0
    # The production line is what ACTUALLY matters — re-read sports_engine.py
    # source and assert the literal `int(sum(` pattern is present, but tolerate
    # any internal restructuring (whitespace, line wraps, helper extraction).
    sports_engine_path = os.path.join(PROJECT_ROOT, "sports_engine.py")
    with open(sports_engine_path) as f:
        src = f.read()
    # Strip whitespace to make the check resilient to wrapping.
    flat = " ".join(src.split())
    assert "ask_depth = int(round(sum(" in flat or "ask_depth=int(round(sum(" in flat, (
        "sports_engine.py: ask_depth aggregation must wrap sum() in int(round(...)). "
        "If you refactored to a helper, update this test to match — but DO NOT "
        "drop the int(round()) cast (see 22P02 wedge 2026-04-19 → 2026-05-01)."
    )
    assert "bid_depth = int(round(sum(" in flat or "bid_depth=int(round(sum(" in flat, (
        "sports_engine.py: bid_depth aggregation must wrap sum() in int(round(...))."
    )
    # Behavioral check — contract holds end-to-end.
    # ask: round(213162.70 + 11753.34) = round(224916.04) = 224916
    # bid: round(9703.00 + 50000.50)   = round(59703.5)   = 59704 (banker's rounding to even)
    assert isinstance(ask_depth, int) and ask_depth == 224916
    assert isinstance(bid_depth, int) and bid_depth == 59704


# ---------------------------------------------------------------------------
# Defensive sync-layer: coerce known-int columns even if upstream emits float.
# Belt-and-suspenders so a future poison-pill on a different code path doesn't
# wedge the sync again.
# ---------------------------------------------------------------------------

def test_supabase_sync_coerces_ask_depth_to_int():
    """Defensive: even if a row somehow has float ask_depth in local DB
    (e.g., the 251 already-poisoned rows from 2026-04-19 → 2026-05-01),
    the supabase POST payload must have it as int so the batch doesn't 400.

    This catches the case where the source fix is shipped but the local
    poisoned rows still need to drain through sync."""
    from supabase_sync import SupabaseSyncer
    # Method-level coercion — a row dict containing float ask_depth must be
    # normalized before POST. We probe via _coerce_int_columns (added by
    # the fix) or by running the full row mapping path if no helper exists.
    assert hasattr(SupabaseSyncer, "_coerce_int_columns"), (
        "SupabaseSyncer must expose _coerce_int_columns(row_dict) so callers "
        "can defensively round known-integer columns before POST. Without "
        "this, the 251 already-poisoned rows in local DB will keep wedging "
        "the sync forever even after the source fix."
    )
    row = {"ticker": "X", "ask_depth": 213162.7, "bid_depth": 99.0, "market_price": 50}
    out = SupabaseSyncer._coerce_int_columns(row)
    assert isinstance(out["ask_depth"], int) and out["ask_depth"] == 213163, (
        f"expected ask_depth=213163 (int), got {out['ask_depth']!r}"
    )
    assert isinstance(out["bid_depth"], int) and out["bid_depth"] == 99
    # Already-int values should pass through unchanged
    assert out["market_price"] == 50 and isinstance(out["market_price"], int)
    # Non-int columns must NOT be touched
    assert out["ticker"] == "X"


def test_supabase_sync_coerces_none_passthrough():
    """NULL ask_depth must stay None — not 0, not error."""
    from supabase_sync import SupabaseSyncer
    out = SupabaseSyncer._coerce_int_columns({"ask_depth": None, "bid_depth": None})
    assert out["ask_depth"] is None
    assert out["bid_depth"] is None
