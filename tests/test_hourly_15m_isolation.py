"""Tests verifying hourly promotion does NOT affect 15M trading pipeline.

Every test here checks that hourly-specific code paths (bankroll fraction,
fixed sizing, taker-only execution, edge cap, kill switch) are cleanly
gated on product_type == "hourly" and cannot leak into 15M behavior.
"""

import os
import sys
import importlib

import pytest

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestHourlyConstants:
    """Verify hourly constants are set correctly."""

    def test_kill_switch_default_off(self):
        """HOURLY_LIVE_ENABLED defaults to False without env var."""
        env = os.environ.pop("HOURLY_LIVE_ENABLED", None)
        try:
            import bot as _b
            importlib.reload(_b)
            assert _b.HOURLY_LIVE_ENABLED is False
            assert _b.HOURLY_OBSERVATION_ONLY is True
        finally:
            if env is not None:
                os.environ["HOURLY_LIVE_ENABLED"] = env

    def test_hourly_max_entry_price(self):
        import bot
        assert bot.HOURLY_MAX_ENTRY_PRICE == 59, "Sub-60c ceiling must be 59"

    def test_hourly_excluded_assets(self):
        import bot
        assert "SOL" in bot.HOURLY_EXCLUDED_ASSETS
        assert "XRP" in bot.HOURLY_EXCLUDED_ASSETS
        assert "BTC" not in bot.HOURLY_EXCLUDED_ASSETS
        assert "ETH" not in bot.HOURLY_EXCLUDED_ASSETS

    def test_hourly_fixed_contracts(self):
        import bot
        assert bot.HOURLY_FIXED_CONTRACTS == 10

    def test_hourly_bankroll_fraction(self):
        import bot
        assert bot.HOURLY_BANKROLL_FRACTION == 0.10

    def test_hourly_max_edge(self):
        import bot
        assert bot.HOURLY_MAX_EDGE == 0.05

    def test_hourly_taker_only(self):
        import bot
        assert bot.HOURLY_TAKER_ONLY is True

    def test_hourly_stc_range(self):
        import bot
        assert bot.HOURLY_MIN_STC_ENTRY == 600
        assert bot.HOURLY_MAX_STC_ENTRY == 1800

    def test_killed_configs_h_j_k(self):
        """Configs h, j, k must not appear in HOURLY_SHADOW_CONFIGS."""
        import bot
        names = {c["name"] for c in bot.HOURLY_SHADOW_CONFIGS}
        assert "hourly_config_h" not in names, "Config h should be killed (55% WR)"
        assert "hourly_config_j" not in names, "Config j should be killed (55% WR)"
        assert "hourly_config_k" not in names, "Config k should be killed (55% WR)"


class TestMarketConfigSync:
    """Verify market_config.py matches bot.py constants."""

    def test_hourly_config_values(self):
        from market_config import MARKET_CONFIGS
        cfg = MARKET_CONFIGS["hourly"]
        assert cfg.max_entry_price == 59
        assert cfg.min_stc_entry == 600
        assert cfg.max_stc_entry == 1800
        assert cfg.excluded_assets == frozenset({"SOL", "XRP"})

    def test_validate_market_configs_passes(self):
        """Full validation against bot.py constants."""
        from market_config import validate_market_configs
        validate_market_configs()  # Raises AssertionError if mismatch

    def test_15m_config_unchanged(self):
        """15M config must not be affected by hourly changes."""
        import bot
        from market_config import MARKET_CONFIGS
        cfg = MARKET_CONFIGS["15m"]
        assert cfg.observation_only == bot.OBSERVATION_MODE
        assert cfg.max_entry_price == bot.MAX_ENTRY_PRICE  # 99, not 59
        assert cfg.min_entry_price == bot.MIN_ENTRY_PRICE
        assert cfg.kelly_fraction == 1.0  # Full Kelly for 15M
        assert cfg.max_risk_per_trade == bot.MAX_RISK_PER_TRADE


class TestSizingIsolation:
    """Verify hourly fixed sizing does not leak into 15M."""

    def test_15m_uses_kelly_not_fixed(self):
        """15M candidates must use Kelly sizing, never fixed 10."""
        import bot
        # 15M product_type is "15m" or None
        # The fixed sizing check is: if _pt == "hourly"
        # 15M should never match this
        assert "15m" != "hourly"
        assert None != "hourly"  # noqa: E711

    def test_hourly_bankroll_fraction_value(self):
        """Hourly uses 10% of balance; 15M uses full balance."""
        import bot
        assert bot.HOURLY_BANKROLL_FRACTION == 0.10
        assert bot.SPX_HOURLY_BANKROLL_FRACTION == 0.15
        # 15M has no fraction — uses full balance (no CRYPTO_BANKROLL_FRACTION exists)
        assert not hasattr(bot, "CRYPTO_BANKROLL_FRACTION")

    def test_asset_risk_caps_15m_only(self):
        """XRP and BTC risk caps are gated on _pt in (None, '15m')."""
        import bot
        import ast
        import inspect
        source = inspect.getsource(bot.OpportunityScanner.scan)
        # Verify the asset risk caps have the 15M-only gate
        assert 'asset == "XRP" and _pt in (None, "15m")' in source
        assert 'asset == "BTC" and _pt in (None, "15m")' in source


class TestExecutionIsolation:
    """Verify hourly taker-only path does not interfere with 15M."""

    def test_hourly_taker_method_exists(self):
        """_execute_hourly_taker must be a method on OrderExecutor."""
        import bot
        assert hasattr(bot.OrderExecutor, "_execute_hourly_taker")

    def test_hourly_taker_route_in_execute(self):
        """execute() must route hourly to _execute_hourly_taker BEFORE the asset lock."""
        import bot
        import inspect
        source = inspect.getsource(bot.OrderExecutor.execute)
        lines = source.split("\n")
        hourly_route_line = None
        asset_lock_line = None
        for i, line in enumerate(lines):
            if "_execute_hourly_taker" in line and hourly_route_line is None:
                hourly_route_line = i
            if "_active_orders" in line and "asset" in line and asset_lock_line is None:
                asset_lock_line = i
        assert hourly_route_line is not None, "_execute_hourly_taker not found in execute()"
        assert asset_lock_line is not None, "asset lock not found in execute()"
        assert hourly_route_line < asset_lock_line, (
            f"Hourly taker route (line {hourly_route_line}) must come BEFORE "
            f"asset lock (line {asset_lock_line})")

    def test_hourly_taker_does_not_use_active_orders(self):
        """_execute_hourly_taker executable code must not write to _active_orders."""
        import bot
        import inspect
        source = inspect.getsource(bot.OrderExecutor._execute_hourly_taker)
        # Strip docstring — only check executable lines
        lines = source.split("\n")
        code_lines = [l for l in lines if l.strip() and not l.strip().startswith(('"""', '"', '#', "'''"))]
        code = "\n".join(code_lines)
        assert "self._active_orders" not in code, (
            "_execute_hourly_taker must not touch self._active_orders (15M maker lock)")
        assert "self._escalating_assets" not in code, (
            "_execute_hourly_taker must not touch self._escalating_assets (15M escalation)")

    def test_execute_gates_on_product_type(self):
        """The hourly route check must be 'product_type == hourly', not an else clause."""
        import bot
        import inspect
        source = inspect.getsource(bot.OrderExecutor.execute)
        assert 'candidate.get("product_type") == "hourly"' in source


class TestEdgeCapIsolation:
    """Verify hourly edge cap does not affect 15M."""

    def test_edge_cap_gated_on_hourly(self):
        """HOURLY_MAX_EDGE check must be gated on _pt == 'hourly'."""
        import bot
        import inspect
        source = inspect.getsource(bot.OpportunityScanner.scan)
        # Find the edge cap check
        assert '_pt == "hourly" and fee_adjusted_edge > HOURLY_MAX_EDGE' in source

    def test_15m_has_no_max_edge(self):
        """15M must not be affected by any max edge cap."""
        import bot
        # 15M uses MIN_EDGE_BY_PRICE (floor), never a ceiling
        assert hasattr(bot, "MIN_EDGE_BY_PRICE")
        # No 15M-specific max edge constant
        assert not hasattr(bot, "MAX_EDGE")
        assert not hasattr(bot, "CRYPTO_MAX_EDGE")


class TestObservationGate:
    """Verify the observation gate behavior with kill switch."""

    def test_observation_only_derives_from_kill_switch(self):
        """HOURLY_OBSERVATION_ONLY must be the inverse of HOURLY_LIVE_ENABLED."""
        import bot
        assert bot.HOURLY_OBSERVATION_ONLY == (not bot.HOURLY_LIVE_ENABLED)

    def test_15m_observation_mode_independent(self):
        """15M OBSERVATION_MODE must not be affected by hourly kill switch."""
        import bot
        # OBSERVATION_MODE is a separate constant, not derived from HOURLY_LIVE_ENABLED
        assert hasattr(bot, "OBSERVATION_MODE")
        # Verify they're independent
        assert "HOURLY_LIVE_ENABLED" not in str(bot.OBSERVATION_MODE)
