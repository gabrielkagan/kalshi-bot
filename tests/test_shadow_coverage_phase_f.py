"""Phase F (Shadow Coverage Expansion): resolution metadata + cross-asset
spot snapshot + funding-rate population.

Phase F populates the Phase-B-added Tier 3 enrichment columns on every 15M
`evaluated_opportunities` row:

  Cross-asset spot snapshot (4): btc/eth/sol/xrp_spot_at_decision — current
    spot of each asset at the decision tick (absolute price; complementary
    to existing relative `btc_spot_change_*_bps`).

  Resolution-path metadata (4 of 5):
    - `final_spot_price` — current spot at decision tick. Each downstream
      ON-CONFLICT-UPDATE overwrites it; the final stored value is the
      spot at the LAST decision tick, a close proxy for spot-at-settlement.
    - `max_excursion_from_strike` — signed price excursion from strike at
      the most extreme point. Positive = max above, negative = max below;
      whichever has larger absolute magnitude wins.
    - `time_above_strike_seconds`, `time_below_strike_seconds` — total
      cumulative seconds the spot was above/below strike since the window
      opened. Derived from new accumulators in `_window_states`.

  DEFERRED (Phase F-2 / future): `knockout_time_relative` requires explicit
  knockout-event detection; OKX/Deribit funding rates require
  exchange-specific funding feeds (CoinGlass returns AVG only). These
  columns stay NULL post-Phase-F.

Master plan: kb/decisions/shadow-coverage-expansion-may01.md.
"""

import os
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)


class TestPhaseFCrossAssetSpotSnapshot:
    """All cross-asset spots are populated at decision tick when the
    primary feed has live prices.
    T1 (2026-05-10): expanded to 6 assets (added HYPE/DOGE shadow observation)."""

    def test_compute_cross_asset_spot_returns_all_four(self):
        import bot

        # Stub a feed that returns prices.
        class _StubFeed:
            def __init__(self):
                self._prices = {
                    "BTC": 67400.0, "ETH": 3210.5,
                    "SOL": 142.7, "XRP": 2.51,
                    "HYPE": 43.01, "DOGE": 0.11,
                }
            def get_all_prices(self):
                return dict(self._prices)
            def get_price(self, asset):  # legacy fallback
                return self._prices.get(asset)

        class _Stub:
            _feed = _StubFeed()
        _Stub._compute_cross_asset_spot_snapshot = bot.OpportunityScanner._compute_cross_asset_spot_snapshot

        feats = _Stub._compute_cross_asset_spot_snapshot(_Stub)
        assert feats == {
            "btc_spot_at_decision": 67400.0,
            "eth_spot_at_decision": 3210.5,
            "sol_spot_at_decision": 142.7,
            "xrp_spot_at_decision": 2.51,
            "hype_spot_at_decision": 43.01,
            "doge_spot_at_decision": 0.11,
        }

    def test_compute_cross_asset_spot_handles_missing_feed_data(self):
        """If feed returns None for an asset, that key stays None."""
        import bot

        class _StubFeed:
            def get_all_prices(self):
                return {"BTC": 67400.0, "ETH": None, "SOL": None, "XRP": None}
            def get_price(self, asset):
                return 67400.0 if asset == "BTC" else None

        class _Stub:
            _feed = _StubFeed()
        _Stub._compute_cross_asset_spot_snapshot = bot.OpportunityScanner._compute_cross_asset_spot_snapshot

        feats = _Stub._compute_cross_asset_spot_snapshot(_Stub)
        assert feats["btc_spot_at_decision"] == 67400.0
        assert feats["eth_spot_at_decision"] is None
        assert feats["sol_spot_at_decision"] is None
        assert feats["xrp_spot_at_decision"] is None


class TestPhaseFResolutionMetadataFromWindowStates:
    """`max_excursion_from_strike`, `time_above_strike_seconds`, and
    `time_below_strike_seconds` are derived from `_window_states` —
    extended in Phase F to track total time-above/below accumulators."""

    def test_resolution_features_present_in_compute_window_features(self):
        """The new 3 resolution fields must appear in the dict returned by
        _compute_window_features (sentinels — values are 0/None for a
        not-yet-populated window)."""
        import bot

        class _Stub:
            _compute_window_features = bot.OpportunityScanner._compute_window_features
            _compute_knockout_time_relative = bot.OpportunityScanner._compute_knockout_time_relative
        s = _Stub()
        s._window_states = {}

        # Ticker doesn't exist yet → returns {} (existing behavior).
        feats = s._compute_window_features("NONEXISTENT")
        assert feats == {}

        # Create a window state manually with the new accumulator fields.
        from collections import deque
        s._window_states["TST"] = {
            "spot_at_open": 67000.0,
            "first_above_since": None,
            "max_buf": 1.5,        # +1.5% above strike at peak
            "min_buf": -0.8,       # -0.8% below at trough
            "crossings": deque(),
            "was_above": None,
            "last_ts": __import__("time").time(),
            # Phase F additions
            "time_above_total_s": 120.0,
            "time_below_total_s": 60.0,
            "threshold": 67500.0,
        }
        feats = s._compute_window_features("TST")
        # Phase F additions:
        assert "time_above_strike_seconds" in feats, "time_above_strike_seconds missing"
        assert "time_below_strike_seconds" in feats, "time_below_strike_seconds missing"
        assert "max_excursion_from_strike" in feats, "max_excursion_from_strike missing"

        assert feats["time_above_strike_seconds"] == 120.0
        assert feats["time_below_strike_seconds"] == 60.0
        # max_excursion: max_buf (+1.5%) has larger |val| than min_buf (-0.8%) →
        # signed positive excursion. Magnitude = 1.5% * 67500 / 100 = 1012.5
        assert feats["max_excursion_from_strike"] == pytest.approx(1012.5, rel=1e-6)

    def test_max_excursion_signed_negative_when_min_buf_dominates(self):
        """When min_buf has larger |val| than max_buf, excursion is negative."""
        import bot
        from collections import deque

        class _Stub:
            _compute_window_features = bot.OpportunityScanner._compute_window_features
            _compute_knockout_time_relative = bot.OpportunityScanner._compute_knockout_time_relative
        s = _Stub()
        s._window_states = {}

        s._window_states["TST"] = {
            "spot_at_open": 67000.0,
            "first_above_since": None,
            "max_buf": 0.3,          # +0.3% above
            "min_buf": -2.0,         # -2.0% below — larger |
            "crossings": deque(),
            "was_above": None,
            "last_ts": __import__("time").time(),
            "time_above_total_s": 0.0,
            "time_below_total_s": 300.0,
            "threshold": 50000.0,
        }
        feats = s._compute_window_features("TST")
        # Magnitude = 2.0% * 50000 / 100 = 1000. Sign negative.
        assert feats["max_excursion_from_strike"] == pytest.approx(-1000.0, rel=1e-6)


class TestPhaseFWindowStateAccumulators:
    """`_update_window_state` must update time_above_total_s and
    time_below_total_s accumulators based on dt between calls."""

    def test_update_accumulates_time_above(self):
        """Two consecutive calls with above-strike spots increment
        time_above_total_s by the dt between them."""
        import bot

        class _Stub:
            _window_states = {}
        _Stub._update_window_state = bot.OpportunityScanner._update_window_state

        # First tick: spot above strike. is_above=True; on the very first
        # call there's no prior state so nothing to accumulate yet.
        _Stub._update_window_state(_Stub, "TST", spot=67500.0, threshold=67000.0)
        first_state = _Stub._window_states["TST"]
        assert first_state.get("time_above_total_s", 0.0) >= 0.0

        # Force a known dt by mutating last_ts back.
        first_state["last_ts"] -= 5.0  # pretend 5s ago
        _Stub._update_window_state(_Stub, "TST", spot=67600.0, threshold=67000.0)
        # Should have added ~5s to time_above_total_s.
        delta_above = first_state["time_above_total_s"]
        assert 4.0 <= delta_above <= 6.0, (
            f"expected ~5s in time_above_total_s, got {delta_above}"
        )

    def test_update_accumulates_time_below(self):
        import bot

        class _Stub:
            _window_states = {}
        _Stub._update_window_state = bot.OpportunityScanner._update_window_state

        _Stub._update_window_state(_Stub, "TST", spot=66500.0, threshold=67000.0)
        first_state = _Stub._window_states["TST"]
        first_state["last_ts"] -= 3.0
        _Stub._update_window_state(_Stub, "TST", spot=66600.0, threshold=67000.0)
        delta_below = first_state["time_below_total_s"]
        assert 2.0 <= delta_below <= 4.0, (
            f"expected ~3s in time_below_total_s, got {delta_below}"
        )

    def test_update_records_threshold_for_excursion_calc(self):
        """Phase F adds `threshold` to the per-window state so the
        excursion calc can scale buf_pct → price units."""
        import bot

        class _Stub:
            _window_states = {}
        _Stub._update_window_state = bot.OpportunityScanner._update_window_state
        _Stub._update_window_state(_Stub, "TST", spot=67500.0, threshold=67000.0)
        assert _Stub._window_states["TST"].get("threshold") == 67000.0


class TestPhaseFFinalSpotPriceAutoFill:
    """Phase F adversarial review MEDIUM-1 regression: `final_spot_price`
    must populate from the caller's `spot_price` kwarg INDEPENDENTLY of
    `_extended_feature_provider`. Pre-fix: the assignment was inside the
    `if _ext:` block, so non-15M rows or provider-failure paths got
    NULL. Post-fix: the assignment is outside that block."""

    def test_final_spot_price_populated_when_provider_returns_empty(self):
        """Non-15M product_type causes provider to return {}; the auto-fill
        must still write final_spot_price from caller's spot_price."""
        import bot
        sm = bot.StateManager(":memory:")
        # Provider returns empty dict (mimics non-15M product_type or
        # provider failure swallowed by the inner try/except).
        sm._extended_feature_provider = lambda *a, **k: {}
        sm.insert_evaluated_opportunity(
            ticker="TEST-FSP-1", event_ticker="E", asset="BTC",
            filter_stage="hourly_observation", product_type="hourly",
            spot_price=67432.5,
        )
        row = sm.conn.execute(
            "SELECT final_spot_price FROM evaluated_opportunities "
            "WHERE ticker = 'TEST-FSP-1'"
        ).fetchone()
        assert row is not None and row["final_spot_price"] == pytest.approx(67432.5), (
            f"final_spot_price should populate from caller's spot_price "
            f"even when provider returns {{}}. Got {row and row['final_spot_price']!r}. "
            f"Pre-Phase-F-round-2 the assignment was inside `if _ext:` "
            f"and would silently drop to NULL."
        )

    def test_final_spot_price_populated_when_no_provider_at_all(self):
        """When _extended_feature_provider is None (e.g. early startup),
        final_spot_price must still populate."""
        import bot
        sm = bot.StateManager(":memory:")
        # No provider registered at all.
        assert sm._extended_feature_provider is None
        sm.insert_evaluated_opportunity(
            ticker="TEST-FSP-2", event_ticker="E", asset="BTC",
            filter_stage="hourly_observation", product_type="hourly",
            spot_price=67500.0,
        )
        row = sm.conn.execute(
            "SELECT final_spot_price FROM evaluated_opportunities "
            "WHERE ticker = 'TEST-FSP-2'"
        ).fetchone()
        assert row is not None and row["final_spot_price"] == pytest.approx(67500.0)


class TestPhaseFEndToEndProvider:
    """The 7 new fields (4 cross-asset + 3 resolution) flow through
    `_get_extended_features_for_ticker` and into evaluated_opportunities
    via the auto-fill block."""

    def test_provider_returns_phase_f_fields(self):
        """The provider's returned dict must contain all 7 Phase F keys
        when both window_state and feed are populated."""
        import bot
        from collections import deque

        class _StubFeed:
            def get_all_prices(self):
                return {"BTC": 67400.0, "ETH": 3210.5, "SOL": 142.7, "XRP": 2.51}
            def get_price(self, asset):  # legacy fallback
                return self.get_all_prices().get(asset)
            def get_buffer(self, asset):
                return []  # Tier 2 momentum returns {} — fine

        # Use an INSTANCE (not class) so the descriptor protocol binds
        # `self` properly when the provider does `self._compute_*(...)`.
        class _Stub:
            _compute_window_features = bot.OpportunityScanner._compute_window_features
            _compute_knockout_time_relative = bot.OpportunityScanner._compute_knockout_time_relative
            _compute_momentum_features = bot.OpportunityScanner._compute_momentum_features
            _compute_cross_asset_features = bot.OpportunityScanner._compute_cross_asset_features
            _compute_bot_state_features = bot.OpportunityScanner._compute_bot_state_features
            _compute_cross_asset_spot_snapshot = bot.OpportunityScanner._compute_cross_asset_spot_snapshot
            _get_extended_features_for_ticker = bot.OpportunityScanner._get_extended_features_for_ticker
        s = _Stub()
        s._feed = _StubFeed()
        s._window_states = {
            "TST": {
                "spot_at_open": 67000.0,
                "first_above_since": None,
                "max_buf": 1.5,
                "min_buf": -0.5,
                "crossings": deque(),
                "was_above": None,
                "last_ts": __import__("time").time(),
                "time_above_total_s": 100.0,
                "time_below_total_s": 50.0,
                "threshold": 67500.0,
            }
        }
        s._bot_state_cache = {}
        s._ml = None
        s._state = None  # bot_state_features will fail, isolated by per-helper try/except

        feats = s._get_extended_features_for_ticker(
            "TST", "BTC", spot_price=67500.0, threshold=67500.0,
            product_type="15m",
        )
        # Phase F cross-asset
        assert feats.get("btc_spot_at_decision") == 67400.0
        assert feats.get("eth_spot_at_decision") == 3210.5
        assert feats.get("sol_spot_at_decision") == 142.7
        assert feats.get("xrp_spot_at_decision") == 2.51
        # Phase F resolution
        assert feats.get("time_above_strike_seconds") == 100.0
        assert feats.get("time_below_strike_seconds") == 50.0
        assert feats.get("max_excursion_from_strike") == pytest.approx(1012.5, rel=1e-6)
