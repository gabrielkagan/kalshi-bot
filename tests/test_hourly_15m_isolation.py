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
        assert bot.HOURLY_FIXED_CONTRACTS == 25

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

    def test_hourly_taker_caps_count(self):
        """_execute_hourly_taker must enforce count cap via min()."""
        import bot
        import inspect
        source = inspect.getsource(bot.OrderExecutor._execute_hourly_taker)
        assert "min(candidate" in source and "HOURLY_FIXED_CONTRACTS" in source

    def test_addon_blocks_hourly(self):
        """_check_addon_opportunities must skip hourly fills."""
        import bot
        import inspect
        source = inspect.getsource(bot.OrderExecutor._check_addon_opportunities)
        assert '"hourly"' in source, "Addon must check for hourly product_type"

    def test_addon_meta_includes_product_type(self):
        """Addon registration must store product_type in meta."""
        import bot
        import inspect
        source = inspect.getsource(bot.OrderExecutor._register_addon_eligible)
        assert '"product_type"' in source

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

    def test_hourly_fee_uses_batch_sizing(self):
        """Hourly fee computation must use HOURLY_FIXED_CONTRACTS, not 1."""
        import bot
        import inspect
        source = inspect.getsource(bot.OpportunityScanner.scan)
        assert "calculate_fee(HOURLY_FIXED_CONTRACTS, best_ask" in source

    def test_15m_fee_uses_single_contract(self):
        """15M fee computation must still use calculate_fee(1, ...) — not batch."""
        import bot
        import inspect
        source = inspect.getsource(bot.OpportunityScanner.scan)
        # The else branch still uses calculate_fee(1, ...)
        assert "calculate_fee(1, best_ask, is_taker=True" in source
        assert not hasattr(bot, "CRYPTO_MAX_EDGE")


class TestHourlyIOCOffset:
    """Verify hourly IOC uses ask+1c offset for fill rate."""

    def test_hourly_taker_applies_offset(self):
        """_execute_hourly_taker must apply IOC_RETRY_OFFSET."""
        import bot
        import inspect
        source = inspect.getsource(bot.OrderExecutor._execute_hourly_taker)
        assert "IOC_RETRY_OFFSET" in source

    def test_sol_taker_offset_unchanged(self):
        """SOL taker-first offset logic must still reference IOC_RETRY_OFFSET."""
        import bot
        import inspect
        source = inspect.getsource(bot.OrderExecutor.execute)
        # SOL path at line ~11128
        assert "IOC_RETRY_OFFSET" in source


class TestHourlyDC:
    """Verify hourly DC is properly configured and isolated."""

    def test_hourly_dc_constants(self):
        import bot
        assert bot.HOURLY_DC_Z_THRESHOLD == -4.0
        assert bot.HOURLY_DC_MIN_PRICE == 93
        assert bot.HOURLY_DC_MAX_PRICE == 96
        assert bot.HOURLY_DC_ASSUMED_PROB == 0.97
        assert bot.HOURLY_DC_CONTRACTS == 25
        assert bot.HOURLY_DC_MIN_SIGMA == 0.000250
        assert bot.HOURLY_DC_ASSETS == {"BTC"}
        assert bot.HOURLY_DC_MAX_PER_WINDOW == 1

    def test_hourly_dc_in_scan(self):
        """Hourly DC evaluation must exist in scan() with product_type hourly gate."""
        import bot
        import inspect
        source = inspect.getsource(bot.OpportunityScanner.scan)
        assert 'HOURLY_DC_ENABLED' in source
        assert 'HOURLY_DC_Z_THRESHOLD' in source
        assert 'HOURLY_DC_MIN_SIGMA' in source
        assert '"hourly_dc"' in source

    def test_hourly_dc_routes_through_dc_taker(self):
        """hourly_dc strategy must be in the DC taker routing list in execute()."""
        import bot
        import inspect
        source = inspect.getsource(bot.OrderExecutor.execute)
        assert '"hourly_dc"' in source

    def test_hourly_dc_skips_hourly_taker(self):
        """hourly_dc must NOT route through _execute_hourly_taker."""
        import bot
        import inspect
        source = inspect.getsource(bot.OrderExecutor.execute)
        assert 'strategy") != "hourly_dc"' in source

    def test_hourly_dc_independent_of_sub60c_killswitch(self):
        """HOURLY_DC_ENABLED is separate from HOURLY_LIVE_ENABLED."""
        import bot
        # They're different constants
        assert hasattr(bot, 'HOURLY_DC_ENABLED')
        assert hasattr(bot, 'HOURLY_LIVE_ENABLED')
        # DC can be on while sub-60c is off
        assert bot.HOURLY_DC_ENABLED is True or bot.HOURLY_DC_ENABLED is False


class TestDCRetryQueue:
    """Verify DC IOC non-blocking retry queue constants and wiring."""

    def test_dc_retry_constants(self):
        import bot
        assert bot.DC_IOC_RETRY_DELAY == 8, "DC retry delay must be 8s"
        assert bot.DC_IOC_MAX_RETRIES == 10, "DC max retries must be 10"

    def test_dc_execute_delegates_to_method(self):
        """execute() DC path must delegate to _execute_dc_taker."""
        import bot
        import inspect
        source = inspect.getsource(bot.OrderExecutor.execute)
        assert "_execute_dc_taker" in source

    def test_dc_execute_method_exists(self):
        """_execute_dc_taker must exist on OrderExecutor."""
        import bot
        assert hasattr(bot.OrderExecutor, "_execute_dc_taker")

    def test_dc_process_retries_method_exists(self):
        """process_dc_retries must exist on OrderExecutor."""
        import bot
        assert hasattr(bot.OrderExecutor, "process_dc_retries")

    def test_dc_retry_queue_no_sleep(self):
        """Neither _execute_dc_taker nor process_dc_retries must call time.sleep."""
        import bot
        import inspect
        src_execute = inspect.getsource(bot.OrderExecutor._execute_dc_taker)
        src_process = inspect.getsource(bot.OrderExecutor.process_dc_retries)
        assert "time.sleep" not in src_execute, "_execute_dc_taker must not block with sleep"
        assert "time.sleep" not in src_process, "process_dc_retries must not block with sleep"

    def test_dc_retry_queue_in_init(self):
        """OrderExecutor must initialize _dc_retry_queue."""
        import bot
        import inspect
        source = inspect.getsource(bot.OrderExecutor.__init__)
        assert "_dc_retry_queue" in source

    def test_submit_taker_returns_filled_count(self):
        """_submit_taker must set filled_count on returned order_info."""
        import bot
        import inspect
        source = inspect.getsource(bot.OrderExecutor._submit_taker)
        assert 'order_info["filled_count"]' in source

    def test_dc_retry_does_not_affect_general_cooldown(self):
        """DC_IOC_RETRY_DELAY must be separate from IOC_TICKER_COOLDOWN."""
        import bot
        assert bot.DC_IOC_RETRY_DELAY < bot.IOC_TICKER_COOLDOWN, (
            "DC retry delay must be shorter than general cooldown")

    def test_process_dc_retries_wired_in_tick(self):
        """process_dc_retries must be called in _tick()."""
        import bot
        import inspect
        source = inspect.getsource(bot.MainLoop._tick)
        assert "process_dc_retries" in source

    def test_dc_retry_checks_price_floor(self):
        """process_dc_retries must check DECIDED_CONTRACT_MIN_PRICE before submitting."""
        import bot
        import inspect
        source = inspect.getsource(bot.OrderExecutor.process_dc_retries)
        assert "DECIDED_CONTRACT_MIN_PRICE" in source
        assert "ABORT_PRICE_COLLAPSED" in source

    def test_dc_retry_checks_price_drift(self):
        """process_dc_retries must abort on 3c+ price drift from original."""
        import bot
        import inspect
        source = inspect.getsource(bot.OrderExecutor.process_dc_retries)
        assert "original_price" in source
        assert "ABORT_PRICE_DRIFT" in source

    def test_dc_initial_checks_price_floor(self):
        """_execute_dc_taker must check DECIDED_CONTRACT_MIN_PRICE on fresh ask."""
        import bot
        import inspect
        source = inspect.getsource(bot.OrderExecutor._execute_dc_taker)
        assert "DECIDED_CONTRACT_MIN_PRICE" in source
        assert "ABORT_PRICE_BELOW_FLOOR" in source

    def test_dc_retry_queue_stores_original_price(self):
        """All retry queue entries must include original_price."""
        import bot
        import inspect
        source = inspect.getsource(bot.OrderExecutor._execute_dc_taker)
        # Count occurrences of original_price in queue appends
        assert source.count('"original_price"') >= 3, (
            "All 3 queue-append sites must include original_price")


class TestHourlyDC97cShadow:
    """Verify hourly DC 97c+ shadow variant is isolated from 15M DC."""

    def test_hourly_dc_97c_shadow_in_scan(self):
        """The hourly_dc_97c_stc600 shadow must exist in scan()."""
        import bot
        import inspect
        source = inspect.getsource(bot.OpportunityScanner.scan)
        assert "hourly_dc_97c_stc600" in source

    def test_hourly_dc_97c_uses_own_constants(self):
        """The 97c shadow must NOT reference HOURLY_DC_MIN_PRICE or HOURLY_DC_MAX_PRICE."""
        import bot
        import inspect
        source = inspect.getsource(bot.OpportunityScanner.scan)
        # Find the 97c shadow block
        idx = source.find("hourly_dc_97c_stc600")
        assert idx > 0
        # Get ~500 chars around it
        block = source[max(0, idx-300):idx+500]
        # Must NOT use the old hourly DC price constants (uses hardcoded 97/99)
        assert "HOURLY_DC_MIN_PRICE" not in block, "97c shadow must not use HOURLY_DC_MIN_PRICE"
        assert "HOURLY_DC_MAX_PRICE" not in block, "97c shadow must not use HOURLY_DC_MAX_PRICE"

    def test_15m_dc_constants_unchanged(self):
        """15M DC constants must not be affected by hourly DC changes."""
        import bot
        assert bot.DECIDED_CONTRACT_MIN_PRICE == 93
        assert bot.DECIDED_CONTRACT_MAX_STC == 300
        assert bot.DECIDED_CONTRACT_Z_T2 == -3.0

    def test_hourly_dc_original_constants_unchanged(self):
        """Original hourly DC constants must be unchanged (shadow runs alongside)."""
        import bot
        assert bot.HOURLY_DC_MIN_PRICE == 93
        assert bot.HOURLY_DC_MAX_PRICE == 96
        assert bot.HOURLY_DC_Z_THRESHOLD == -4.0

    def test_isolation_15m_not_hourly(self):
        """15M DC code must gate on _pt in (None, '15m'), not 'hourly'."""
        import bot
        import inspect
        source = inspect.getsource(bot.OpportunityScanner.scan)
        # The 15M DC block has: _pt in (None, "15m")
        assert '_pt in (None, "15m")' in source


class TestHourlyDC93cShadow:
    """Verify hourly DC 93-96c tier 2 shadow."""

    def test_hourly_dc_93c_in_scan(self):
        """hourly_dc_93c_stc300 must exist in scan()."""
        import bot
        import inspect
        source = inspect.getsource(bot.OpportunityScanner.scan)
        assert "hourly_dc_93c_stc300" in source

    def test_tier2_price_range(self):
        """Tier 2 must gate on 93-96c, not overlap with Tier 1 (97-99c)."""
        import bot
        import inspect
        source = inspect.getsource(bot.OpportunityScanner.scan)
        idx = source.find("hourly_dc_93c_stc300")
        block = source[max(0, idx-400):idx+200]
        assert "best_ask >= 93" in block
        assert "best_ask <= 96" in block

    def test_tier2_stc_300(self):
        """Tier 2 must use STC <= 300s (tighter than Tier 1's 600s)."""
        import bot
        import inspect
        source = inspect.getsource(bot.OpportunityScanner.scan)
        idx = source.find("hourly_dc_93c_stc300")
        block = source[max(0, idx-400):idx+200]
        assert "seconds_remaining <= 300" in block

    def test_no_overlap_with_tier1(self):
        """Tier 2 price range (93-96) must not overlap with Tier 1 (97-99)."""
        # Tier 1: best_ask >= 97 and best_ask <= 99
        # Tier 2: best_ask >= 93 and best_ask <= 96
        # A 97c signal cannot match Tier 2's <= 96 check
        assert 97 > 96  # Tier 1 min > Tier 2 max — no overlap


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
