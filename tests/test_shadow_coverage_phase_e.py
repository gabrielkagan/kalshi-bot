"""Phase E (Shadow Coverage Expansion): state-at-decision-time features.

Phase E populates the 3 Phase-B-added columns that capture the bot's risk
posture at scan time:
  - `n_open_positions` (INTEGER): total open positions across ALL assets
    (distinct from existing `active_positions_same_asset` which is
    same-asset only).
  - `recent_n_outcome_streak` (INTEGER, signed): streak count from the
    most recent settled trade. Positive = consecutive wins, negative =
    consecutive losses, 0 = no recent trades or alternating.
  - `time_since_last_fill_s` (REAL): seconds since the last position
    opened (MAX(opened_at) on positions). NULL if no positions ever.

Implementation: extend `_compute_bot_state_features` (bot/_impl.py around
line 10638) which is already cached at 60s + per-asset and feeds
`_extended_feature_provider`. The 3 new fields are GLOBAL (not
per-asset) but stored in the per-asset cache for uniform access; all
assets in the same tick read the same values.

Master plan: kb/decisions/shadow-coverage-expansion-may01.md (Phase E).
"""

import os
import sqlite3
import sys
import tempfile

import pytest
import bot.scanner  # noqa: F401
import bot.state  # noqa: F401

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)


class TestPhaseEStateAtDecisionInsert:
    """End-to-end: when insert_evaluated_opportunity fires for a 15M row
    via the provider, the 3 Phase-E fields are populated from current
    bot state."""

    def _build_state_with_positions(self, n_open_total: int):
        """Build a StateManager with `n_open_total` open positions distributed
        across BTC/ETH/SOL/XRP, return (sm, asset_breakdown)."""
        import bot
        # Use :memory: so we don't pollute the real DB.
        sm = bot.state.StateManager(":memory:")
        # Insert N positions across assets — round-robin so the total is
        # known and `active_positions_same_asset` differs from the total.
        assets = ["BTC", "ETH", "SOL", "XRP"]
        for i in range(n_open_total):
            asset = assets[i % len(assets)]
            sm.conn.execute(
                "INSERT INTO positions(ticker, event_ticker, asset, side, count, "
                "avg_price_cents, total_cost_cents, opened_at, updated_at, status) "
                "VALUES (?, 'EVT-X', ?, 'yes', 1, 80, 80, "
                "datetime('now', '-' || ? || ' seconds'), "
                "datetime('now', '-' || ? || ' seconds'), 'open')",
                (f"TEST-{i}", asset, i + 1, i + 1),
            )
        sm.conn.commit()
        return sm

    def test_n_open_positions_is_total_across_assets(self):
        """`n_open_positions` reflects total across all 4 assets, not the
        same-asset count."""
        import bot
        sm = self._build_state_with_positions(n_open_total=7)

        # Construct a minimal MainLoop-like object that exposes
        # _compute_bot_state_features. We don't need a full MainLoop —
        # just an object with the methods the provider uses. Use a
        # subclass with the real method but stub deps.
        class _Stub:
            ASSETS = ["BTC", "ETH", "SOL", "XRP"]
            _bot_state_cache = {}
            _ml = None
            _state = sm
        _Stub._compute_bot_state_features = bot.scanner.OpportunityScanner._compute_bot_state_features
        feats = _Stub._compute_bot_state_features(_Stub, "BTC")

        assert feats.get("n_open_positions") == 7, (
            f"n_open_positions should be total across assets (7), "
            f"got {feats.get('n_open_positions')!r}"
        )
        # active_positions_same_asset should be the per-asset count (BTC = 2 of 7)
        # Round-robin: BTC=ix 0,4 → 2; ETH=1,5 → 2; SOL=2,6 → 2; XRP=3 → 1
        assert feats.get("active_positions_same_asset") == 2, (
            f"active_positions_same_asset for BTC should be 2 (round-robin), "
            f"got {feats.get('active_positions_same_asset')!r}"
        )

    def test_time_since_last_fill_is_positive(self):
        """When there's at least one open position, time_since_last_fill_s
        is a non-negative number (seconds since most recent opened_at)."""
        import bot
        sm = self._build_state_with_positions(n_open_total=3)

        class _Stub:
            ASSETS = ["BTC", "ETH", "SOL", "XRP"]
            _bot_state_cache = {}
            _ml = None
            _state = sm
        _Stub._compute_bot_state_features = bot.scanner.OpportunityScanner._compute_bot_state_features
        feats = _Stub._compute_bot_state_features(_Stub, "BTC")

        tslf = feats.get("time_since_last_fill_s")
        assert tslf is not None, "time_since_last_fill_s should not be None when positions exist"
        assert tslf >= 0, f"time_since_last_fill_s must be non-negative, got {tslf!r}"
        # The freshest position was inserted with `'-1 seconds'`, so
        # tslf should be ~1s ± clock skew.
        assert tslf < 60, f"time_since_last_fill_s expected ~1s, got {tslf!r}"

    def test_time_since_last_fill_none_when_no_positions(self):
        """When no positions have ever existed, time_since_last_fill_s is None."""
        import bot
        sm = bot.state.StateManager(":memory:")

        class _Stub:
            ASSETS = ["BTC", "ETH", "SOL", "XRP"]
            _bot_state_cache = {}
            _ml = None
            _state = sm
        _Stub._compute_bot_state_features = bot.scanner.OpportunityScanner._compute_bot_state_features
        feats = _Stub._compute_bot_state_features(_Stub, "BTC")
        assert feats.get("time_since_last_fill_s") is None, (
            f"time_since_last_fill_s should be None when no positions exist, "
            f"got {feats.get('time_since_last_fill_s')!r}"
        )

    def test_recent_n_outcome_streak_consecutive_wins(self):
        """Three consecutive wins → +3 streak."""
        import bot
        sm = bot.state.StateManager(":memory:")
        # Three settled trades, all wins (positive net pnl).
        for i in range(3):
            sm.conn.execute(
                "INSERT INTO settled_trades(ticker, event_ticker, asset, market_result, "
                "side, count, entry_price_cents, revenue_cents, fee_cents, pnl_cents, "
                "settled_at) VALUES (?, 'E', 'BTC', 'yes', 'yes', 1, 80, 100, 1, 20, "
                "datetime('now', '-' || ? || ' seconds'))",
                (f"WIN-{i}", i * 10),
            )
        sm.conn.commit()

        class _Stub:
            ASSETS = ["BTC", "ETH", "SOL", "XRP"]
            _bot_state_cache = {}
            _ml = None
            _state = sm
        _Stub._compute_bot_state_features = bot.scanner.OpportunityScanner._compute_bot_state_features
        feats = _Stub._compute_bot_state_features(_Stub, "BTC")
        assert feats.get("recent_n_outcome_streak") == 3, (
            f"3 consecutive wins → streak +3, got {feats.get('recent_n_outcome_streak')!r}"
        )

    def test_recent_n_outcome_streak_consecutive_losses(self):
        """Two consecutive losses → -2 streak."""
        import bot
        sm = bot.state.StateManager(":memory:")
        for i in range(2):
            sm.conn.execute(
                "INSERT INTO settled_trades(ticker, event_ticker, asset, market_result, "
                "side, count, entry_price_cents, revenue_cents, fee_cents, pnl_cents, "
                "settled_at) VALUES (?, 'E', 'BTC', 'no', 'yes', 1, 80, 0, 1, -81, "
                "datetime('now', '-' || ? || ' seconds'))",
                (f"LOSS-{i}", i * 10),
            )
        sm.conn.commit()

        class _Stub:
            ASSETS = ["BTC", "ETH", "SOL", "XRP"]
            _bot_state_cache = {}
            _ml = None
            _state = sm
        _Stub._compute_bot_state_features = bot.scanner.OpportunityScanner._compute_bot_state_features
        feats = _Stub._compute_bot_state_features(_Stub, "BTC")
        assert feats.get("recent_n_outcome_streak") == -2, (
            f"2 consecutive losses → streak -2, got {feats.get('recent_n_outcome_streak')!r}"
        )

    def test_recent_n_outcome_streak_breaks_on_alternation(self):
        """Most recent: WIN, then LOSS — streak = +1 (only the most recent
        is counted; the loss before it is a different sign)."""
        import bot
        sm = bot.state.StateManager(":memory:")
        # Older loss
        sm.conn.execute(
            "INSERT INTO settled_trades(ticker, event_ticker, asset, market_result, "
            "side, count, entry_price_cents, revenue_cents, fee_cents, pnl_cents, "
            "settled_at) VALUES ('OLD-LOSS', 'E', 'BTC', 'no', 'yes', 1, 80, 0, 1, -81, "
            "datetime('now', '-300 seconds'))"
        )
        # Newer win
        sm.conn.execute(
            "INSERT INTO settled_trades(ticker, event_ticker, asset, market_result, "
            "side, count, entry_price_cents, revenue_cents, fee_cents, pnl_cents, "
            "settled_at) VALUES ('NEW-WIN', 'E', 'BTC', 'yes', 'yes', 1, 80, 100, 1, 20, "
            "datetime('now', '-10 seconds'))"
        )
        sm.conn.commit()

        class _Stub:
            ASSETS = ["BTC", "ETH", "SOL", "XRP"]
            _bot_state_cache = {}
            _ml = None
            _state = sm
        _Stub._compute_bot_state_features = bot.scanner.OpportunityScanner._compute_bot_state_features
        feats = _Stub._compute_bot_state_features(_Stub, "BTC")
        assert feats.get("recent_n_outcome_streak") == 1, (
            f"WIN-after-LOSS streak should be +1 (most-recent only), "
            f"got {feats.get('recent_n_outcome_streak')!r}"
        )

    def test_recent_n_outcome_streak_zero_when_no_trades(self):
        """No settled trades → streak is 0."""
        import bot
        sm = bot.state.StateManager(":memory:")

        class _Stub:
            ASSETS = ["BTC", "ETH", "SOL", "XRP"]
            _bot_state_cache = {}
            _ml = None
            _state = sm
        _Stub._compute_bot_state_features = bot.scanner.OpportunityScanner._compute_bot_state_features
        feats = _Stub._compute_bot_state_features(_Stub, "BTC")
        assert feats.get("recent_n_outcome_streak") == 0, (
            f"no trades → streak 0, got {feats.get('recent_n_outcome_streak')!r}"
        )


class TestPhaseEReconciliationSurvival:
    """Phase E adversarial review MEDIUM-1 regression: time_since_last_fill_s
    must survive reconciliation row deletes. bot/_impl.py issues
    `DELETE FROM positions WHERE ticker=?` when Kalshi REST reports
    position_count=0. Pre-fix: tslf would drop to NULL despite recent
    activity. Post-fix: settled_trades is also queried, so tslf reflects
    `MAX(positions.opened_at, settled_trades.settled_at)`."""

    def test_tslf_survives_position_table_clear(self):
        import bot
        sm = bot.state.StateManager(":memory:")
        # Seed a settled trade — recent.
        sm.conn.execute(
            "INSERT INTO settled_trades(ticker, event_ticker, asset, market_result, "
            "side, count, entry_price_cents, revenue_cents, fee_cents, pnl_cents, "
            "settled_at) VALUES ('SETTLED-X', 'E', 'BTC', 'yes', 'yes', 1, 80, 100, 1, 20, "
            "datetime('now', '-5 seconds'))"
        )
        # Positions table is empty (simulating reconciliation just cleared it).
        sm.conn.commit()

        class _Stub:
            ASSETS = ["BTC", "ETH", "SOL", "XRP"]
            _bot_state_cache = {}
            _ml = None
            _state = sm
        _Stub._compute_bot_state_features = bot.scanner.OpportunityScanner._compute_bot_state_features

        feats = _Stub._compute_bot_state_features(_Stub, "BTC")
        tslf = feats.get("time_since_last_fill_s")
        assert tslf is not None, (
            f"tslf should fall back to settled_trades.settled_at when "
            f"positions is empty (reconciliation post-clear). Got None — "
            f"the MEDIUM-1 fix is missing or broken."
        )
        assert 0 <= tslf < 60, f"tslf expected ~5s, got {tslf!r}"


class TestPhaseEStreakPushSemantics:
    """Phase E adversarial review LOW-1 regression: pushes (net pnl == 0)
    must BREAK the streak (treated as neutral), not contribute as losses."""

    def test_push_then_wins_breaks_streak(self):
        """Most recent: PUSH. Older: WIN, WIN. Streak should be 0 (push
        most-recent yields 0 by definition; pre-fix would have been -1
        with push counted as loss)."""
        import bot
        sm = bot.state.StateManager(":memory:")
        # 2 older wins
        for i in range(2):
            sm.conn.execute(
                "INSERT INTO settled_trades(ticker, event_ticker, asset, market_result, "
                "side, count, entry_price_cents, revenue_cents, fee_cents, pnl_cents, "
                "settled_at) VALUES (?, 'E', 'BTC', 'yes', 'yes', 1, 80, 100, 1, 20, "
                "datetime('now', '-' || ? || ' seconds'))",
                (f"WIN-{i}", 100 + i * 10),
            )
        # Most recent: PUSH (revenue == fee, net pnl == 0).
        sm.conn.execute(
            "INSERT INTO settled_trades(ticker, event_ticker, asset, market_result, "
            "side, count, entry_price_cents, revenue_cents, fee_cents, pnl_cents, "
            "settled_at) VALUES ('PUSH', 'E', 'BTC', 'yes', 'yes', 1, 80, 1, 1, 1, "
            "datetime('now', '-1 seconds'))"
        )
        sm.conn.commit()

        class _Stub:
            ASSETS = ["BTC", "ETH", "SOL", "XRP"]
            _bot_state_cache = {}
            _ml = None
            _state = sm
        _Stub._compute_bot_state_features = bot.scanner.OpportunityScanner._compute_bot_state_features

        feats = _Stub._compute_bot_state_features(_Stub, "BTC")
        assert feats.get("recent_n_outcome_streak") == 0, (
            f"PUSH most-recent should yield streak=0 (per LOW-1 fix); "
            f"got {feats.get('recent_n_outcome_streak')!r}"
        )

    def test_wins_then_push_in_middle_breaks_streak(self):
        """Most recent: WIN. Then: PUSH (older). Then: WIN, WIN. Streak
        should be +1 (the most-recent win, then push breaks)."""
        import bot
        sm = bot.state.StateManager(":memory:")
        for i in range(2):
            sm.conn.execute(
                "INSERT INTO settled_trades(ticker, event_ticker, asset, market_result, "
                "side, count, entry_price_cents, revenue_cents, fee_cents, pnl_cents, "
                "settled_at) VALUES (?, 'E', 'BTC', 'yes', 'yes', 1, 80, 100, 1, 20, "
                "datetime('now', '-' || ? || ' seconds'))",
                (f"OLD-WIN-{i}", 100 + i * 10),
            )
        # Middle: PUSH
        sm.conn.execute(
            "INSERT INTO settled_trades(ticker, event_ticker, asset, market_result, "
            "side, count, entry_price_cents, revenue_cents, fee_cents, pnl_cents, "
            "settled_at) VALUES ('PUSH', 'E', 'BTC', 'yes', 'yes', 1, 80, 1, 1, 1, "
            "datetime('now', '-50 seconds'))"
        )
        # Most recent: WIN
        sm.conn.execute(
            "INSERT INTO settled_trades(ticker, event_ticker, asset, market_result, "
            "side, count, entry_price_cents, revenue_cents, fee_cents, pnl_cents, "
            "settled_at) VALUES ('NEW-WIN', 'E', 'BTC', 'yes', 'yes', 1, 80, 100, 1, 20, "
            "datetime('now', '-1 seconds'))"
        )
        sm.conn.commit()

        class _Stub:
            ASSETS = ["BTC", "ETH", "SOL", "XRP"]
            _bot_state_cache = {}
            _ml = None
            _state = sm
        _Stub._compute_bot_state_features = bot.scanner.OpportunityScanner._compute_bot_state_features

        feats = _Stub._compute_bot_state_features(_Stub, "BTC")
        assert feats.get("recent_n_outcome_streak") == 1, (
            f"WIN-then-PUSH (older) should yield streak=+1 (push breaks); "
            f"got {feats.get('recent_n_outcome_streak')!r}"
        )


class TestPhaseEProviderWiresIntoInsert:
    """The provider must propagate the 3 fields end-to-end into an
    actually-stored `evaluated_opportunities` row."""

    def test_15m_insert_populates_phase_e_fields(self):
        """Build a StateManager, set up positions + a settlement, register
        the provider, run insert_evaluated_opportunity, read back."""
        import bot
        sm = bot.state.StateManager(":memory:")
        # Seed positions + 1 settled win.
        sm.conn.execute(
            "INSERT INTO positions(ticker, event_ticker, asset, side, count, "
            "avg_price_cents, total_cost_cents, opened_at, updated_at, status) "
            "VALUES ('POS-X', 'E', 'BTC', 'yes', 1, 80, 80, "
            "datetime('now', '-5 seconds'), datetime('now'), 'open')"
        )
        sm.conn.execute(
            "INSERT INTO settled_trades(ticker, event_ticker, asset, market_result, "
            "side, count, entry_price_cents, revenue_cents, fee_cents, pnl_cents, "
            "settled_at) VALUES ('OLD-WIN', 'E', 'BTC', 'yes', 'yes', 1, 80, 100, 1, 20, "
            "datetime('now', '-30 seconds'))"
        )
        sm.conn.commit()

        # Wire a minimal provider stub mimicking what MainLoop does.
        class _Stub:
            ASSETS = ["BTC", "ETH", "SOL", "XRP"]
            _bot_state_cache = {}
            _ml = None
            _state = sm
        _Stub._compute_bot_state_features = bot.scanner.OpportunityScanner._compute_bot_state_features

        def _provider(ticker, asset, spot, threshold, product_type):
            if product_type not in (None, "15m"):
                return {}
            if asset is None:
                return {}
            try:
                return _Stub._compute_bot_state_features(_Stub, asset)
            except Exception:
                return {}

        sm._extended_feature_provider = _provider
        sm.insert_evaluated_opportunity(
            ticker="TEST15M-X", event_ticker="E", asset="BTC",
            filter_stage="low_price_shadow",
            product_type="15m", market_price=50,
        )
        row = sm.conn.execute(
            "SELECT n_open_positions, time_since_last_fill_s, "
            "recent_n_outcome_streak FROM evaluated_opportunities "
            "WHERE ticker = 'TEST15M-X'"
        ).fetchone()
        assert row is not None
        assert row["n_open_positions"] == 1, (
            f"expected n_open_positions=1, got {row['n_open_positions']!r}"
        )
        assert row["time_since_last_fill_s"] is not None
        assert row["time_since_last_fill_s"] >= 0
        assert row["recent_n_outcome_streak"] == 1, (
            f"expected streak +1 (1 prior win), got "
            f"{row['recent_n_outcome_streak']!r}"
        )
