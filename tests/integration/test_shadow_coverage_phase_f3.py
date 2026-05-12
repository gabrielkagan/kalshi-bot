"""Phase F-3 (Shadow Coverage Expansion): knockout_time_relative.

Approximate decision-time formula:
  knockout_time_relative = (now - last_crossing_ts) / (now - window_open_ts)
  bounded [0, 1].

Semantic: how long the market has been "decided" since the last
strike crossing, normalized by total window-elapsed time. Range:
  - 1.0 = no crossings since window opened (decided from the start)
  - ~0.0 = a crossing JUST happened (market still in flux)
  - intermediate = market was decided X% of its observation window

DEFERRED: a settlement-time backfill via SettlementTracker would
produce a more accurate "knockout time" relative to the FULL window
(including post-decision-tick activity). Phase F-3 ships the
decision-tick approximation; future refinement is a Phase F-3b.

Master plan: kb/decisions/shadow-coverage-expansion-may01.md.
"""

import os
import sys
import time

import pytest
import bot.scanner  # noqa: F401
import bot.state  # noqa: F401

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)


class TestPhaseF3KnockoutHelper:
    """Helper computes knockout_time_relative from a window-state dict."""

    def _state(self, **overrides):
        from collections import deque
        base = {
            "spot_at_open": 67000.0,
            "first_above_since": None,
            "max_buf": 1.5,
            "min_buf": -0.5,
            "crossings": deque(),
            "was_above": True,
            "last_ts": time.time(),
            "time_above_total_s": 100.0,
            "time_below_total_s": 50.0,
            "threshold": 67500.0,
            "window_open_ts": time.time() - 600.0,  # 10 min ago
        }
        base.update(overrides)
        return base

    def test_no_crossings_returns_one(self):
        """No crossings since window-open → market decided since open → 1.0."""
        import bot
        import bot.scanner  # noqa: F401 (Bit 9.3-iii.c — explicit submodule import; bot.scanner.X access)
        st = self._state()
        out = bot.scanner.OpportunityScanner._compute_knockout_time_relative(None,
            st, now=time.time(),
        )
        assert out == pytest.approx(1.0, abs=0.01)

    def test_one_crossing_at_window_open_returns_one(self):
        """First crossing right at window open is degenerate (no decided
        time before it). Result still ~1.0 because (now - crossing) ==
        (now - window_open)."""
        import bot
        import bot.scanner  # noqa: F401 (Bit 9.3-iii.c — explicit submodule import; bot.scanner.X access)
        from collections import deque
        now = time.time()
        st = self._state(
            crossings=deque([now - 600.0]),
            window_open_ts=now - 600.0,
        )
        out = bot.scanner.OpportunityScanner._compute_knockout_time_relative(None, st, now=now)
        assert out == pytest.approx(1.0, abs=0.02)

    def test_recent_crossing_returns_near_zero(self):
        """Crossing 5s ago, window 600s old → (5 / 600) ≈ 0.0083."""
        import bot
        import bot.scanner  # noqa: F401 (Bit 9.3-iii.c — explicit submodule import; bot.scanner.X access)
        from collections import deque
        now = time.time()
        st = self._state(
            crossings=deque([now - 590.0, now - 5.0]),
            window_open_ts=now - 600.0,
        )
        out = bot.scanner.OpportunityScanner._compute_knockout_time_relative(None, st, now=now)
        assert out == pytest.approx(5.0 / 600.0, rel=0.01)

    def test_intermediate_crossing_returns_intermediate(self):
        """Crossing halfway through the window → 0.5."""
        import bot
        import bot.scanner  # noqa: F401 (Bit 9.3-iii.c — explicit submodule import; bot.scanner.X access)
        from collections import deque
        now = time.time()
        st = self._state(
            crossings=deque([now - 300.0]),
            window_open_ts=now - 600.0,
        )
        out = bot.scanner.OpportunityScanner._compute_knockout_time_relative(None, st, now=now)
        assert out == pytest.approx(0.5, abs=0.01)

    def test_window_open_in_future_returns_none(self):
        """Defensive: clock-skew or stale state where window_open_ts is in
        the future → can't compute → None (rather than producing negative
        or > 1 value)."""
        import bot
        import bot.scanner  # noqa: F401 (Bit 9.3-iii.c — explicit submodule import; bot.scanner.X access)
        now = time.time()
        st = self._state(window_open_ts=now + 10.0)
        out = bot.scanner.OpportunityScanner._compute_knockout_time_relative(None, st, now=now)
        assert out is None

    def test_missing_window_open_ts_returns_none(self):
        """Pre-Phase-F-3 window states (in-memory carryover from a long-
        running session that booted on the older code) lack window_open_ts.
        Helper must return None, not crash."""
        import bot
        import bot.scanner  # noqa: F401 (Bit 9.3-iii.c — explicit submodule import; bot.scanner.X access)
        st = self._state()
        del st["window_open_ts"]
        out = bot.scanner.OpportunityScanner._compute_knockout_time_relative(None,
            st, now=time.time(),
        )
        assert out is None

    def test_clamps_to_one(self):
        """Numerical edge: if last crossing is BEFORE window_open (shouldn't
        happen but be defensive), value would be > 1. Clamp."""
        import bot
        import bot.scanner  # noqa: F401 (Bit 9.3-iii.c — explicit submodule import; bot.scanner.X access)
        from collections import deque
        now = time.time()
        st = self._state(
            crossings=deque([now - 1200.0]),  # 20 min ago
            window_open_ts=now - 600.0,        # but window opened 10 min ago
        )
        out = bot.scanner.OpportunityScanner._compute_knockout_time_relative(None, st, now=now)
        assert out == 1.0


class TestPhaseF3WindowStateRecordsOpenTs:
    """`_update_window_state` must record `window_open_ts` on first call."""

    def test_first_call_records_window_open_ts(self):
        import bot
        import bot.scanner  # noqa: F401 (Bit 9.3-iii.c — explicit submodule import; bot.scanner.X access)

        class _Stub:
            _window_states = {}
        _Stub._update_window_state = bot.scanner.OpportunityScanner._update_window_state

        before = time.time()
        _Stub._update_window_state(_Stub, "TST", spot=67500.0, threshold=67000.0)
        after = time.time()
        st = _Stub._window_states["TST"]
        assert "window_open_ts" in st
        assert before <= st["window_open_ts"] <= after

    def test_subsequent_calls_preserve_window_open_ts(self):
        """The window-open timestamp is set ONCE — subsequent ticks must
        not overwrite it (the state opens once, ticks update otherwise)."""
        import bot
        import bot.scanner  # noqa: F401 (Bit 9.3-iii.c — explicit submodule import; bot.scanner.X access)

        class _Stub:
            _window_states = {}
        _Stub._update_window_state = bot.scanner.OpportunityScanner._update_window_state

        _Stub._update_window_state(_Stub, "TST", spot=67500.0, threshold=67000.0)
        original_open = _Stub._window_states["TST"]["window_open_ts"]
        time.sleep(0.05)
        _Stub._update_window_state(_Stub, "TST", spot=67600.0, threshold=67000.0)
        assert _Stub._window_states["TST"]["window_open_ts"] == original_open


class TestPhaseF3ComputeWindowFeaturesIncludesKnockout:
    """`_compute_window_features` must surface knockout_time_relative
    so it flows through the auto-fill block to evaluated_opportunities."""

    def test_compute_window_features_includes_knockout(self):
        import bot
        import bot.scanner  # noqa: F401 (Bit 9.3-iii.c — explicit submodule import; bot.scanner.X access)
        from collections import deque

        class _Stub:
            _compute_window_features = bot.scanner.OpportunityScanner._compute_window_features
            _compute_knockout_time_relative = bot.scanner.OpportunityScanner._compute_knockout_time_relative
        # Instance — descriptor protocol binds `self` correctly when
        # _compute_window_features calls self._compute_knockout_time_relative.
        s = _Stub()
        s._window_states = {}

        now = time.time()
        s._window_states["TST"] = {
            "spot_at_open": 67000.0,
            "first_above_since": None,
            "max_buf": 1.5,
            "min_buf": -0.5,
            "crossings": deque([now - 60.0]),  # 1 min ago
            "was_above": True,
            "last_ts": now,
            "time_above_total_s": 540.0,
            "time_below_total_s": 60.0,
            "threshold": 67500.0,
            "window_open_ts": now - 600.0,  # 10 min ago
        }
        feats = s._compute_window_features("TST")
        assert "knockout_time_relative" in feats
        # 60s of decided time / 600s window = 0.1.
        assert feats["knockout_time_relative"] == pytest.approx(0.1, rel=0.05)


class TestPhaseF3EndToEndInsert:
    """End-to-end: an insert_evaluated_opportunity call with a configured
    extended_feature_provider that returns knockout_time_relative populates
    the column."""

    def test_insert_picks_up_knockout_from_provider(self):
        import bot
        import bot.state  # noqa: F401 (Bit 9.3-iii.c — explicit submodule import; bot.state.X access)
        sm = bot.state.StateManager(":memory:")
        sm._extended_feature_provider = lambda *a, **k: {
            "knockout_time_relative": 0.42,
        }
        sm.insert_evaluated_opportunity(
            ticker="TEST15M-K", event_ticker="E", asset="BTC",
            filter_stage="low_price_shadow", product_type="15m",
        )
        row = sm.conn.execute(
            "SELECT knockout_time_relative FROM evaluated_opportunities "
            "WHERE ticker = 'TEST15M-K'"
        ).fetchone()
        assert row is not None
        assert row["knockout_time_relative"] == pytest.approx(0.42, rel=1e-6)
