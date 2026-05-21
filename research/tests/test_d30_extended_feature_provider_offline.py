"""D-30 — _extended_feature_provider callbacks aren't reachable in replay.

Authoritative source: bot._impl::insert_evaluated_opportunity calls a
scanner-provided callback (self._extended_feature_provider) to auto-fill
Tier 1/2/3/6 features (window-state, momentum, cross-asset, bot-state).
These are computed live from in-memory state (orderbook caches, position
ledger, etc.) — they CANNOT be reconstructed offline.

Replay can use these features for filtering / strategy evaluation IF the
column is non-NULL. It must NEVER attempt to compute them offline.

Test surface: AST regex against any `def _compute_<feature>` for known
extended-feature names in research/replay.py.
"""
from __future__ import annotations

import inspect
import re

import research.replay as rep


# Sample of extended-feature column names per RCA D-30 list.
EXTENDED_FEATURE_NAMES = frozenset({
    "bid_depth",
    "ask_depth",
    "yes_spread_cents",
    "spot_coinbase_kraken_gap_bps",
    "spot_price",
    "momentum_5min",
    "ms_cache",
    "cx_gap",
    "ob_ladder",
    "ms_z_5min",
    "ms_z_15min",
    "ms_z_30min",
    "ms_zone",
    "cx_zscore_15min",
})


def test_d30_no_compute_function_for_extended_features() -> None:
    """research/replay.py does NOT define _compute_<feature> for any extended-feature name."""
    src = inspect.getsource(rep)
    for feat in EXTENDED_FEATURE_NAMES:
        pattern = rf"def\s+_?compute_{re.escape(feat)}\b"
        matches = re.findall(pattern, src)
        assert not matches, (
            f"D-30 offline-compute violation: replay.py defines `_compute_{feat}` "
            f"or similar. Extended features must NOT be reconstructed offline."
        )


def test_d30_no_orderbook_reconstruction_logic_in_replay() -> None:
    """Heuristic: replay.py doesn't reference orderbook reconstruction primitives."""
    src = inspect.getsource(rep)
    forbidden_keywords = [
        "OrderBook",
        "Orderbook",
        "L2Snapshot",
        "OrderbookLadder",
        "OrderFlowEngine",
        "FlowAccumulator",
    ]
    for kw in forbidden_keywords:
        assert kw not in src, (
            f"D-30 orderbook reconstruction in replay.py: {kw!r} found. "
            f"Orderbook state is live-only; replay must use stored snapshot values."
        )


def test_d30_no_imports_from_bot_engines() -> None:
    """replay.py does not import from bot.engines.* (where extended feature logic lives)."""
    src = inspect.getsource(rep)
    forbidden_imports = [
        "from bot.engines",
        "import bot.engines",
        "from bot.scanner",
        "import bot.scanner",
        "from bot.executor",
        "import bot.executor",
        "from bot.order_flow",
        "import bot.order_flow",
    ]
    for imp in forbidden_imports:
        assert imp not in src, (
            f"D-30 bot-engine import in replay.py: {imp!r}. "
            f"Replay is parallel-track and must not couple to live engine code."
        )


def test_d30_replay_uses_stored_values_not_computed() -> None:
    """Heuristic: any handling of extended-feature columns reads them from row,
    not from a computation.

    If B3 needs to FILTER on these columns, the pattern should be
    `row.get('bid_depth')` or `row['bid_depth']`, not a function call.
    """
    src = inspect.getsource(rep)
    # If any extended feature column NAME appears, check that the surrounding
    # context is row access, not function-def or function-call patterns.
    for feat in EXTENDED_FEATURE_NAMES:
        # Best-effort: if a `def fn(feat)` or `compute(feat)` appears, flag it.
        # Skip if the feature name doesn't appear at all.
        if feat not in src:
            continue
        # Look for `def _compute_<feat>` or `<feat>(...)` as a function call signature
        bad_pattern = rf"def\s+\w*\b{re.escape(feat)}\b\w*\s*\("
        matches = re.findall(bad_pattern, src)
        assert not matches, (
            f"D-30 extended-feature function: replay.py contains `def ...{feat}...(` — "
            f"replay must consume columns as data, not compute them."
        )
