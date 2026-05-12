"""Tests for Phase 2 extended feature instrumentation (Tier 1, 2, 3, 6).

Guards against:
- Window state missing cleanup → memory leak
- Max/min buf not tracked correctly
- Crossings deque not counting 5-min window
- minutes_above_strike not resetting when spot crosses below
- Momentum features failing on empty buffers
- Cross-asset features crashing when BTC buffer is empty
- Bot state cache not refreshing at 1-min TTL
- Provider callback leaking exceptions
"""

import math
import sys
import os
import time
import unittest
from collections import deque
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import bot
from bot.constants import PRICE_BUFFER_SIZE
from bot.scanner import OpportunityScanner


def _make_scanner_stub() -> OpportunityScanner:
    """Minimal OpportunityScanner with mocked deps for unit testing the
    Tier 1-6 feature methods (no real network, no real DB)."""
    scanner = OpportunityScanner.__new__(OpportunityScanner)
    scanner._feed = MagicMock()
    scanner._feed.get_buffer = MagicMock(return_value=[])
    scanner._feed.get_price = MagicMock(return_value=None)
    scanner._state = MagicMock()
    scanner._state.conn = MagicMock()
    scanner._state.conn.execute = MagicMock(return_value=MagicMock(
        fetchall=MagicMock(return_value=[]),
        fetchone=MagicMock(return_value=(0, 0)),
    ))
    scanner._ml = None
    scanner._window_states = {}
    scanner._bot_state_cache = {"ts": 0.0, "features_by_asset": {}}
    return scanner


class TestPriceBufferSize(unittest.TestCase):
    def test_extended_to_30_min(self):
        """Phase 2 extends PRICE_BUFFER_SIZE from 300 to 1800 for 30-min history."""
        self.assertEqual(PRICE_BUFFER_SIZE, 1800)


class TestWindowState(unittest.TestCase):
    """Tier 1: per-ticker window state tracking."""

    def test_first_tick_initializes_state(self):
        s = _make_scanner_stub()
        s._update_window_state("KXSOL15M-26APR191645-45", spot=85.28, threshold=85.07)
        self.assertIn("KXSOL15M-26APR191645-45", s._window_states)
        state = s._window_states["KXSOL15M-26APR191645-45"]
        self.assertEqual(state["spot_at_open"], 85.28)
        self.assertAlmostEqual(state["max_buf"], (85.28-85.07)/85.07*100, places=3)

    def test_none_spot_does_nothing(self):
        s = _make_scanner_stub()
        s._update_window_state("T1", None, 85.0)
        self.assertEqual(len(s._window_states), 0)

    def test_max_min_buf_tracked(self):
        s = _make_scanner_stub()
        s._update_window_state("T1", spot=86.0, threshold=85.0)  # buf=1.18%
        s._update_window_state("T1", spot=85.5, threshold=85.0)  # buf=0.59%
        s._update_window_state("T1", spot=86.5, threshold=85.0)  # buf=1.76%
        state = s._window_states["T1"]
        self.assertAlmostEqual(state["max_buf"], (86.5-85.0)/85.0*100, places=3)
        self.assertAlmostEqual(state["min_buf"], (85.5-85.0)/85.0*100, places=3)

    def test_crossing_detected(self):
        s = _make_scanner_stub()
        s._update_window_state("T1", spot=86.0, threshold=85.0)  # above
        s._update_window_state("T1", spot=84.0, threshold=85.0)  # below → crossing
        s._update_window_state("T1", spot=86.0, threshold=85.0)  # above → crossing
        state = s._window_states["T1"]
        self.assertEqual(len(state["crossings"]), 2)

    def test_first_above_since_resets_on_cross_below(self):
        s = _make_scanner_stub()
        s._update_window_state("T1", spot=86.0, threshold=85.0)
        self.assertIsNotNone(s._window_states["T1"]["first_above_since"])
        s._update_window_state("T1", spot=84.0, threshold=85.0)
        self.assertIsNone(s._window_states["T1"]["first_above_since"])

    def test_bounded_dict_size(self):
        """Dict should cap at 100 tickers and evict oldest."""
        s = _make_scanner_stub()
        for i in range(105):
            s._update_window_state(f"T{i}", spot=86.0, threshold=85.0)
        self.assertLessEqual(len(s._window_states), 100)

    def test_compute_window_features_empty(self):
        s = _make_scanner_stub()
        out = s._compute_window_features("nonexistent-ticker")
        self.assertEqual(out, {})

    def test_compute_window_features_populated(self):
        s = _make_scanner_stub()
        s._update_window_state("T1", spot=86.0, threshold=85.0)
        out = s._compute_window_features("T1")
        self.assertIn("minutes_above_strike", out)
        self.assertIn("window_max_buf_pct", out)
        self.assertIn("recent_crossings_5m", out)
        self.assertEqual(out["recent_crossings_5m"], 0)


class TestMomentumFeatures(unittest.TestCase):
    """Tier 2: spot momentum from CoinbaseFeed buffer."""

    def test_empty_buffer_returns_empty(self):
        s = _make_scanner_stub()
        s._feed.get_buffer = MagicMock(return_value=[])
        out = s._compute_momentum_features("BTC")
        self.assertEqual(out, {})

    def test_momentum_60s(self):
        now = time.time()
        # Buffer: 70s ago at $100, now at $101 → 100bps up over ~60s
        buf = [(now - 70, 100.0)] + [(now - i, 100.5) for i in range(69, 1, -1)] + [(now, 101.0)]
        s = _make_scanner_stub()
        s._feed.get_buffer = MagicMock(return_value=buf)
        out = s._compute_momentum_features("BTC")
        self.assertIsNotNone(out.get("spot_momentum_60s_bps"))
        # Should be positive (price went up)
        self.assertGreater(out["spot_momentum_60s_bps"], 0)

    def test_15m_range(self):
        now = time.time()
        # High 102, low 98 over 15 min → mid 100, range = 400bps
        buf = [(now - 800, 102.0), (now - 400, 98.0), (now, 100.0)]
        s = _make_scanner_stub()
        s._feed.get_buffer = MagicMock(return_value=buf)
        out = s._compute_momentum_features("BTC")
        self.assertIsNotNone(out.get("spot_realized_range_15m_bps"))
        self.assertAlmostEqual(out["spot_realized_range_15m_bps"], 400.0, places=0)


class TestCrossAssetFeatures(unittest.TestCase):
    """Tier 3: BTC momentum + relative return."""

    def test_empty_btc_buffer_returns_empty(self):
        s = _make_scanner_stub()
        s._feed.get_buffer = MagicMock(return_value=[])
        out = s._compute_cross_asset_features("SOL")
        self.assertEqual(out, {})

    def test_btc_30m_change(self):
        now = time.time()
        buf = [(now - 1800, 75000.0), (now, 76000.0)]  # +133 bps over 30m

        def buf_lookup(asset):
            if asset == "BTC":
                return buf
            return []
        s = _make_scanner_stub()
        s._feed.get_buffer = MagicMock(side_effect=buf_lookup)
        out = s._compute_cross_asset_features("BTC")
        self.assertIsNotNone(out.get("btc_spot_change_30m_bps"))
        self.assertAlmostEqual(out["btc_spot_change_30m_bps"], 133.33, places=0)

    def test_sol_btc_relative_return_for_non_btc(self):
        now = time.time()
        btc_buf = [(now - 1800, 75000.0), (now, 75000.0)]  # BTC flat
        sol_buf = [(now - 1800, 100.0), (now, 101.0)]  # SOL +100bps

        def buf_lookup(asset):
            if asset == "BTC":
                return btc_buf
            if asset == "SOL":
                return sol_buf
            return []
        s = _make_scanner_stub()
        s._feed.get_buffer = MagicMock(side_effect=buf_lookup)
        out = s._compute_cross_asset_features("SOL")
        # SOL outperformed BTC by ~100bps
        self.assertIsNotNone(out.get("sol_btc_relative_return_30m_bps"))
        self.assertGreater(out["sol_btc_relative_return_30m_bps"], 50)


class TestBotStateCache(unittest.TestCase):
    """Tier 6: 1-min cached bot state lookups."""

    def test_cache_hit_avoids_sql(self):
        s = _make_scanner_stub()
        s._bot_state_cache = {
            "ts": time.time(),
            "features_by_asset": {"SOL": {"active_positions_same_asset": 3}},
        }
        out = s._compute_bot_state_features("SOL")
        # Should hit cache, not call SQL
        s._state.conn.execute.assert_not_called()
        self.assertEqual(out["active_positions_same_asset"], 3)

    def test_stale_cache_refreshes(self):
        s = _make_scanner_stub()
        s._bot_state_cache = {"ts": 0.0, "features_by_asset": {}}  # stale
        # Rig the SQL mock to return deterministic values
        s._state.conn.execute = MagicMock(return_value=MagicMock(
            fetchall=MagicMock(return_value=[("SOL", 2)]),
            fetchone=MagicMock(return_value=(500, 10)),
        ))
        out = s._compute_bot_state_features("SOL")
        # Should have called SQL (at least once for the refresh)
        self.assertTrue(s._state.conn.execute.called)


class TestProviderCallback(unittest.TestCase):
    """_get_extended_features_for_ticker routes correctly by product_type."""

    def test_non_15m_returns_empty(self):
        s = _make_scanner_stub()
        out = s._get_extended_features_for_ticker(
            "KXBTCD-26APR19", "BTC", 75000.0, 74500.0, "hourly")
        self.assertEqual(out, {})

    def test_15m_returns_merged_dict(self):
        s = _make_scanner_stub()
        s._update_window_state("KXSOL15M-X", 86.0, 85.0)
        # Mock the feed to return some buffer data
        now = time.time()
        s._feed.get_buffer = MagicMock(return_value=[(now - 60, 85.5), (now, 86.0)])
        out = s._get_extended_features_for_ticker(
            "KXSOL15M-X", "SOL", 86.0, 85.0, "15m")
        # Should have Tier 1 keys at least
        self.assertIn("minutes_above_strike", out)
        self.assertIn("window_max_buf_pct", out)

    def test_none_asset_returns_empty(self):
        s = _make_scanner_stub()
        out = s._get_extended_features_for_ticker("T1", None, 86.0, 85.0, "15m")
        self.assertEqual(out, {})

    def test_exception_doesnt_propagate(self):
        s = _make_scanner_stub()
        # Force a method to raise
        s._compute_window_features = MagicMock(side_effect=RuntimeError("boom"))
        out = s._get_extended_features_for_ticker("T1", "SOL", 86.0, 85.0, "15m")
        # Caller must not see exception
        self.assertIsInstance(out, dict)


if __name__ == "__main__":
    unittest.main()
