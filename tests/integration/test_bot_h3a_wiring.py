"""Phase H-3a: bot/_impl.py integration tests.

Verifies the wiring from `bot/_impl.py` main loop to `MarketObservationsSnapshotter`:
- Schema migration runs at startup (bot/_impl.py main thread, NOT the daemon thread)
- Snapshotter is instantiated with the WS feed + active-tickers provider
- Snapshotter is started after WS feed connects
- Snapshotter is stopped + joined during shutdown
- Failure to instantiate is non-fatal (matches the pattern for spx_engine,
  weather_engine, sports_engine, fifteenm_shadow)

These are AST-based tests rather than runtime tests because bot/_impl.py is too
heavy to import in a unit test (network calls, threads, env vars). The
AST-style guards protect against signature drift / wiring regression at
the same level as `tests/contracts/test_call_sites.py`.

The active-tickers provider helper (`extract_active_15m_tickers`) is
runtime-tested directly via the snapshotter module (NOT via bot/_impl.py).
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

import bot.snapshots.market_observations_snapshotter as mod  # noqa: E402


# ── Helper unit tests (extract_active_15m_tickers) ────────────────────────


def test_extract_active_15m_tickers_basic():
    windows = [
        {
            "product_type": "15m",
            "markets": [{"ticker": "K1"}, {"ticker": "K2"}],
        },
        {
            "product_type": "hourly",
            "markets": [{"ticker": "H1"}],
        },
        {
            "product_type": "15m",
            "markets": [{"ticker": "K3"}],
        },
    ]
    assert mod.extract_active_15m_tickers(windows) == ["K1", "K2", "K3"]


def test_extract_active_15m_tickers_empty():
    assert mod.extract_active_15m_tickers([]) == []
    assert mod.extract_active_15m_tickers(None) == []


def test_extract_active_15m_tickers_skips_malformed():
    windows = [
        "not a dict",
        {"product_type": "15m"},  # no 'markets' key
        {"product_type": "15m", "markets": "not a list"},
        {"product_type": "15m", "markets": [None, "not a dict", {}]},  # bad markets
        {"product_type": "15m", "markets": [{"ticker": ""}, {"ticker": None}]},  # bad tickers
        {"product_type": "15m", "markets": [{"ticker": "K1"}]},  # one good
    ]
    assert mod.extract_active_15m_tickers(windows) == ["K1"]


def test_extract_active_15m_tickers_filters_non_15m():
    windows = [
        {"product_type": "spx_hourly", "markets": [{"ticker": "S1"}]},
        {"product_type": "weather", "markets": [{"ticker": "W1"}]},
        {"product_type": "sports", "markets": [{"ticker": "SP1"}]},
        {"product_type": "15m", "markets": [{"ticker": "K1"}]},
    ]
    assert mod.extract_active_15m_tickers(windows) == ["K1"]


def test_extract_active_15m_tickers_preserves_order():
    """Ticker order matters for downstream batched inserts being
    reproducible across runs."""
    windows = [
        {"product_type": "15m", "markets": [{"ticker": "Z"}, {"ticker": "A"}]},
        {"product_type": "15m", "markets": [{"ticker": "M"}]},
    ]
    assert mod.extract_active_15m_tickers(windows) == ["Z", "A", "M"]


# ── AST regression tests on bot/_impl.py wiring ────────────────────────────────


@pytest.fixture(scope="module")
def bot_py_source() -> str:
    """Bit 9.3 retarget (2026-05-10): MarketObservationsSnapshotter wiring lives
    inside MainLoop.__init__ which moved to bot/main_loop.py. This fixture
    now reads bot/main_loop.py instead of bot/_impl.py."""
    return (ROOT / "bot/main_loop.py").read_text()


@pytest.fixture(scope="module")
def bot_py_tree(bot_py_source) -> ast.AST:
    return ast.parse(bot_py_source)


def test_bot_imports_snapshotter_module(bot_py_source):
    """bot/_impl.py main loop must import the snapshotter (for the wiring to
    do anything). The import is conditional/lazy inside __init__, so we
    grep the source rather than walk top-level imports."""
    has_module = "market_observations_snapshotter" in bot_py_source
    has_class = "MarketObservationsSnapshotter" in bot_py_source
    assert has_module, "bot/_impl.py missing import of market_observations_snapshotter"
    assert has_class, "bot/_impl.py missing reference to MarketObservationsSnapshotter"


def test_bot_calls_ensure_schema_for_market_obs(bot_py_source):
    """The schema migration MUST be called from bot/_impl.py main thread
    (NOT from the daemon thread per round-1 #7 fix). Prove the call
    site exists in bot/_impl.py."""
    # Either as a renamed import (`_moc_ensure_schema(`) or via attribute
    # access (`market_observations_snapshotter.ensure_schema(`) is fine.
    has_call = (
        "_moc_ensure_schema" in bot_py_source
        or "market_observations_snapshotter.ensure_schema" in bot_py_source
        or "snapshotter.ensure_schema" in bot_py_source
    )
    assert has_call, "bot/_impl.py must call ensure_schema from market_observations_snapshotter"


def test_bot_instantiates_snapshotter_attribute(bot_py_source):
    """An attribute on the main loop must hold the snapshotter instance
    (or None if construction failed). Naming convention: `market_obs_snapshotter`
    — matches the existing `kalshi_feed`, `spx_engine`, etc. pattern."""
    has_attr = "self.market_obs_snapshotter" in bot_py_source
    assert has_attr, "main loop missing self.market_obs_snapshotter attribute"


def test_bot_starts_snapshotter_after_ws_feed(bot_py_source):
    """Snapshotter.start() is called after kalshi_feed.start() — the
    snapshotter has nothing to read until the WS is subscribed.
    Defensive: if the WS feed fails to start, the snapshotter shouldn't
    start either (no orderbooks ever populated)."""
    has_start = "market_obs_snapshotter.start()" in bot_py_source
    assert has_start, "main loop must call market_obs_snapshotter.start()"

    ws_start_idx = bot_py_source.find("self.kalshi_feed.start()")
    snap_start_idx = bot_py_source.find("market_obs_snapshotter.start()")
    if ws_start_idx >= 0 and snap_start_idx >= 0:
        assert snap_start_idx > ws_start_idx, (
            "Snapshotter.start() must come AFTER kalshi_feed.start()"
        )


def test_bot_stops_snapshotter_on_shutdown(bot_py_source):
    """Shutdown handler must stop AND join the snapshotter, with a
    bounded timeout (per round-2 stop semantics: ≤15s)."""
    has_stop = "market_obs_snapshotter.stop()" in bot_py_source
    has_join = "market_obs_snapshotter.join(" in bot_py_source
    assert has_stop, "shutdown must call market_obs_snapshotter.stop()"
    assert has_join, "shutdown must call market_obs_snapshotter.join(timeout=...)"


def test_bot_uses_extract_active_15m_tickers_helper(bot_py_source):
    """Wiring must use the unit-tested helper rather than ad-hoc
    list-comprehension. Inlining the comprehension defeats the test
    coverage — a refactor that introduces a dict-iteration bug would
    not be caught by test_extract_active_15m_tickers_*."""
    has_helper = "extract_active_15m_tickers" in bot_py_source
    assert has_helper, (
        "bot/_impl.py must use the extract_active_15m_tickers helper, "
        "not an inline list-comprehension over self._active_windows"
    )


def test_bot_snapshotter_construction_is_failure_tolerant(bot_py_source):
    """Snapshotter init must be wrapped in try/except — the bot must
    keep running even if the snapshotter module fails to import or
    instantiate (matches pattern for spx_engine, weather_engine, etc.).
    Otherwise a typo in the snapshotter module would crash the bot
    on every restart."""
    # Look for the pattern: try ... MarketObservationsSnapshotter ... except.
    # Easier as a string search than ast.walk for this specific pattern.
    idx = bot_py_source.find("MarketObservationsSnapshotter(")
    assert idx >= 0, "snapshotter must be instantiated"
    # Search ~500 chars before for 'try:' and ~500 chars after for 'except'.
    pre = bot_py_source[max(0, idx - 1000):idx]
    post = bot_py_source[idx:idx + 2000]
    has_try = "try:" in pre
    has_except = "except" in post
    assert has_try, "snapshotter instantiation must be inside a try block"
    assert has_except, (
        "snapshotter instantiation must have an except handler "
        "(matches spx_engine, weather_engine pattern)"
    )


def test_bot_stops_snapshotter_before_state_close(bot_py_source):
    """Round-1 wiring #3: shutdown ordering invariant. Snapshotter holds
    its own sqlite connection; if a future refactor reorders shutdown to
    run state.close() first, that's a new connection-lifecycle bug we
    don't want to ship. Catch via AST."""
    stop_idx = bot_py_source.find("market_obs_snapshotter.stop()")
    state_close_idx = bot_py_source.find("self.state.close()")
    if stop_idx >= 0 and state_close_idx >= 0:
        assert stop_idx < state_close_idx, (
            "snapshotter.stop() must come BEFORE self.state.close() in "
            "shutdown sequence"
        )


def test_bot_snapshotter_init_after_kalshi_feed_init(bot_py_source):
    """The snapshotter requires the kalshi_feed reference. Init must
    happen AFTER kalshi_feed is constructed."""
    feed_init_idx = bot_py_source.find("self.kalshi_feed = KalshiFeed(")
    snap_init_idx = bot_py_source.find("MarketObservationsSnapshotter(")
    if feed_init_idx >= 0 and snap_init_idx >= 0:
        assert snap_init_idx > feed_init_idx, (
            "snapshotter must be constructed AFTER kalshi_feed"
        )
