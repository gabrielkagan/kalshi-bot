"""Config & Wiring Consistency Tests.

Failure mode: MarketTypeConfig drifts from bot.py constants -> crash loop on VPS startup.
Past incidents: Multiple — any time a constant was changed in bot.py but not market_config.py.

These tests duplicate what validate_market_configs() does at runtime, but catch it
*before deploy* in CI.
"""

import os
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)


class TestConfigConstantParity:
    """Every MarketTypeConfig field matches its corresponding bot.py constant."""

    def test_validate_market_configs_succeeds(self):
        """The runtime validation function itself should pass."""
        from market_config import validate_market_configs
        validate_market_configs()

    def test_15m_config_matches_bot(self):
        import bot
        from market_config import MARKET_CONFIGS
        cfg = MARKET_CONFIGS["15m"]
        assert cfg.min_entry_price == bot.MIN_ENTRY_PRICE
        assert cfg.max_entry_price == bot.MAX_ENTRY_PRICE
        assert cfg.max_risk_per_trade == bot.MAX_RISK_PER_TRADE
        assert cfg.min_seconds_before_close == bot.MIN_SECONDS_BEFORE_CLOSE
        assert cfg.max_seconds_before_close == bot.MAX_SECONDS_BEFORE_CLOSE
        assert cfg.market_blend_w == bot.MARKET_BLEND_W
        assert cfg.observation_only == bot.OBSERVATION_MODE

    def test_hourly_config_matches_bot(self):
        import bot
        from market_config import MARKET_CONFIGS
        cfg = MARKET_CONFIGS["hourly"]
        assert cfg.observation_only == bot.HOURLY_OBSERVATION_ONLY
        assert cfg.min_entry_price == bot.HOURLY_MIN_ENTRY_PRICE
        assert cfg.max_entry_price == bot.HOURLY_MAX_ENTRY_PRICE
        assert cfg.min_seconds_before_close == bot.HOURLY_MIN_SECONDS_BEFORE_CLOSE
        assert cfg.max_seconds_before_close == bot.HOURLY_MAX_SECONDS_BEFORE_CLOSE
        assert cfg.max_risk_per_trade == bot.HOURLY_MAX_RISK_PER_TRADE
        assert cfg.kelly_fraction == bot.HOURLY_KELLY_FRACTION
        assert cfg.market_blend_w == bot.HOURLY_MARKET_BLEND_W
        assert cfg.temperature_t == bot.HOURLY_TEMPERATURE_T
        assert cfg.temperature_enabled == bot.HOURLY_TEMPERATURE_ENABLED
        assert cfg.min_stc_entry == bot.HOURLY_MIN_STC_ENTRY
        assert cfg.max_stc_entry == bot.HOURLY_MAX_STC_ENTRY
        assert cfg.excluded_assets == frozenset(bot.HOURLY_EXCLUDED_ASSETS)
        assert cfg.max_positions_per_window == bot.HOURLY_MAX_POSITIONS_PER_WINDOW
        assert cfg.max_window_risk == bot.HOURLY_MAX_WINDOW_RISK

    def test_spx_hourly_config_matches_bot(self):
        import bot
        from market_config import MARKET_CONFIGS
        cfg = MARKET_CONFIGS["spx_hourly"]
        assert cfg.observation_only == bot.SPX_HOURLY_OBSERVATION_ONLY
        assert cfg.min_entry_price == bot.SPX_HOURLY_MIN_ENTRY_PRICE
        assert cfg.max_entry_price == bot.SPX_HOURLY_MAX_ENTRY_PRICE
        assert cfg.min_seconds_before_close == bot.SPX_HOURLY_MIN_SECONDS_BEFORE_CLOSE
        assert cfg.max_seconds_before_close == bot.SPX_HOURLY_MAX_SECONDS_BEFORE_CLOSE
        assert cfg.max_risk_per_trade == bot.SPX_HOURLY_MAX_RISK_PER_TRADE
        assert cfg.kelly_fraction == bot.SPX_HOURLY_KELLY_FRACTION
        assert cfg.market_blend_w == bot.SPX_HOURLY_MARKET_BLEND_W
        assert cfg.temperature_t == bot.SPX_HOURLY_TEMPERATURE_T
        assert cfg.fee_multiplier_taker == bot.SPX_HOURLY_FEE_MULTIPLIER_TAKER
        assert cfg.fee_multiplier_maker == bot.SPX_HOURLY_FEE_MULTIPLIER_MAKER
        assert cfg.max_positions_per_window == bot.SPX_HOURLY_MAX_POSITIONS_PER_WINDOW
        assert cfg.max_window_risk == bot.SPX_HOURLY_MAX_WINDOW_RISK

    def test_weather_config_matches_bot(self):
        import bot
        from market_config import MARKET_CONFIGS
        cfg = MARKET_CONFIGS["weather"]
        assert cfg.observation_only == bot.WEATHER_OBSERVATION_ONLY
        assert cfg.min_entry_price == bot.WEATHER_MIN_ENTRY_PRICE
        assert cfg.max_entry_price == bot.WEATHER_MAX_ENTRY_PRICE
        assert cfg.min_seconds_before_close == bot.WEATHER_MIN_SECONDS_BEFORE_CLOSE
        assert cfg.max_seconds_before_close == bot.WEATHER_MAX_SECONDS_BEFORE_CLOSE
        assert cfg.max_risk_per_trade == bot.WEATHER_MAX_RISK_PER_TRADE
        assert cfg.kelly_fraction == bot.WEATHER_KELLY_FRACTION
        assert cfg.market_blend_w == bot.WEATHER_MARKET_BLEND_W

    def test_sports_config_matches_bot(self):
        import bot
        from market_config import MARKET_CONFIGS
        cfg = MARKET_CONFIGS["sports"]
        assert cfg.observation_only == bot.SPORTS_OBSERVATION_ONLY
        assert cfg.observation_only is True, "sports must always be observation-only"
        assert cfg.cal_eligible is False, "sports must not be cal_eligible"


class TestConfigCompleteness:
    """Every product type in MARKET_CONFIGS has valid field values."""

    def test_all_product_types_have_valid_price_range(self):
        from market_config import MARKET_CONFIGS
        for pt, cfg in MARKET_CONFIGS.items():
            assert 0 < cfg.min_entry_price <= cfg.max_entry_price <= 99, (
                f"{pt}: invalid price range [{cfg.min_entry_price}, {cfg.max_entry_price}]")

    def test_all_product_types_have_valid_risk(self):
        from market_config import MARKET_CONFIGS
        for pt, cfg in MARKET_CONFIGS.items():
            assert 0 < cfg.max_risk_per_trade <= 1.0, (
                f"{pt}: max_risk_per_trade={cfg.max_risk_per_trade} out of (0, 1]")

    def test_all_product_types_have_valid_kelly_fraction(self):
        from market_config import MARKET_CONFIGS
        for pt, cfg in MARKET_CONFIGS.items():
            assert 0 < cfg.kelly_fraction <= 1.0, (
                f"{pt}: kelly_fraction={cfg.kelly_fraction} out of (0, 1]")

    def test_all_product_types_have_valid_blend_w(self):
        from market_config import MARKET_CONFIGS
        for pt, cfg in MARKET_CONFIGS.items():
            assert 0 <= cfg.market_blend_w <= 1.0, (
                f"{pt}: market_blend_w={cfg.market_blend_w} out of [0, 1]")

    def test_all_product_types_have_valid_temperature(self):
        from market_config import MARKET_CONFIGS
        for pt, cfg in MARKET_CONFIGS.items():
            assert cfg.temperature_t > 0, (
                f"{pt}: temperature_t={cfg.temperature_t} must be positive")

    def test_all_product_types_have_valid_fee_multipliers(self):
        from market_config import MARKET_CONFIGS
        for pt, cfg in MARKET_CONFIGS.items():
            assert cfg.fee_multiplier_taker >= 0, (
                f"{pt}: negative taker fee={cfg.fee_multiplier_taker}")
            assert cfg.fee_multiplier_maker >= 0, (
                f"{pt}: negative maker fee={cfg.fee_multiplier_maker}")
            assert cfg.fee_multiplier_maker == 0.0, (
                f"{pt}: maker fee should be $0 (Kalshi), got {cfg.fee_multiplier_maker}")

    def test_product_type_field_matches_key(self):
        """Config dict key must match the product_type field inside the config."""
        from market_config import MARKET_CONFIGS
        for key, cfg in MARKET_CONFIGS.items():
            assert cfg.product_type == key, (
                f"Key '{key}' does not match cfg.product_type='{cfg.product_type}'")


class TestMinEdgeByPrice:
    """MIN_EDGE_BY_PRICE schedule invariants."""

    def test_edge_schedule_is_non_decreasing(self):
        """Higher prices should require higher or equal edge."""
        import bot
        schedule = bot.MIN_EDGE_BY_PRICE
        # Schedule is sorted high-to-low by price floor
        for i in range(len(schedule) - 1):
            higher_floor, higher_edge = schedule[i]
            lower_floor, lower_edge = schedule[i + 1]
            assert higher_floor > lower_floor, (
                f"Schedule not sorted: {higher_floor} <= {lower_floor}")
            assert higher_edge >= lower_edge, (
                f"Edge at {higher_floor}c ({higher_edge}) < edge at {lower_floor}c ({lower_edge})")

    def test_get_min_edge_covers_all_valid_prices(self):
        """get_min_edge returns a positive value for all valid prices 1-99."""
        import bot
        for price in range(1, 100):
            edge = bot.get_min_edge(price)
            assert edge > 0, f"get_min_edge({price}) returned {edge}"

    def test_get_min_edge_monotonic(self):
        """Higher prices get higher or equal edge thresholds."""
        import bot
        prev_edge = 0
        for price in range(1, 100):
            edge = bot.get_min_edge(price)
            assert edge >= prev_edge, (
                f"get_min_edge({price})={edge} < get_min_edge({price-1})={prev_edge}")
            prev_edge = edge


class TestObservationModeFlags:
    """Observation-only configs have matching bot.py flags."""

    def test_observation_configs_have_filter_labels(self):
        """Every observation-only config must have a non-empty observation_filter_label."""
        from market_config import MARKET_CONFIGS
        for pt, cfg in MARKET_CONFIGS.items():
            if cfg.observation_only and pt != "15m":
                assert cfg.observation_filter_label, (
                    f"{pt}: observation_only=True but no observation_filter_label")

    def test_15m_has_no_observation_label(self):
        """15m is live trading — should not have an observation label."""
        from market_config import MARKET_CONFIGS
        cfg = MARKET_CONFIGS["15m"]
        if not cfg.observation_only:
            assert cfg.observation_filter_label == "", (
                f"15m is live but has observation_filter_label='{cfg.observation_filter_label}'")


class TestCalEngineInvariants:
    """CalEngine registry safety checks (from validate_market_configs)."""

    def test_15m_never_uses_cal_engine(self):
        from market_config import MARKET_CONFIGS
        cfg = MARKET_CONFIGS["15m"]
        assert not cfg.cal_engine_enabled
        assert cfg.cal_engine_state_path == ""
        assert not cfg.cal_subtypes

    def test_subtypes_and_single_state_path_mutually_exclusive(self):
        """cal_subtypes + cal_engine_state_path is invalid (pick one).

        Note: cal_subtypes + cal_engine_enabled is OK — it means subtype
        engines are enabled for predictions (e.g., weather per-city CalEngines).
        This matches validate_market_configs() in market_config.py (line ~366).
        """
        from market_config import MARKET_CONFIGS
        for pt, cfg in MARKET_CONFIGS.items():
            if cfg.cal_subtypes:
                assert not cfg.cal_engine_state_path, (
                    f"{pt}: has both cal_engine_state_path and cal_subtypes")

    def test_no_duplicate_state_paths(self):
        from market_config import MARKET_CONFIGS
        all_paths = []
        for pt, cfg in MARKET_CONFIGS.items():
            if cfg.cal_engine_state_path:
                all_paths.append(cfg.cal_engine_state_path)
            for sub_path in cfg.cal_subtypes.values():
                all_paths.append(sub_path)
        assert len(all_paths) == len(set(all_paths)), (
            f"Duplicate state paths: {[p for p in all_paths if all_paths.count(p) > 1]}")

    def test_15m_is_cal_eligible(self):
        from market_config import MARKET_CONFIGS
        assert MARKET_CONFIGS["15m"].cal_eligible is True

    def test_non_15m_not_cal_eligible(self):
        from market_config import MARKET_CONFIGS
        for pt in ("hourly", "spx_hourly", "weather", "sports"):
            assert not MARKET_CONFIGS[pt].cal_eligible, f"{pt} should not be cal_eligible"


class TestSTCRanges:
    """STC (seconds-to-close) range validity."""

    def test_stc_ranges_valid(self):
        """min_seconds_before_close < max_seconds_before_close for types that use STC."""
        from market_config import MARKET_CONFIGS
        for pt, cfg in MARKET_CONFIGS.items():
            if cfg.max_seconds_before_close > 0:
                assert cfg.min_seconds_before_close < cfg.max_seconds_before_close, (
                    f"{pt}: min_stc({cfg.min_seconds_before_close}) >= max_stc({cfg.max_seconds_before_close})")

    def test_stc_shadow_within_15m_range(self):
        """STC_SHADOW_THRESHOLD for 15M must fall within the STC range."""
        import bot
        assert bot.STC_SHADOW_THRESHOLD <= bot.MAX_SECONDS_BEFORE_CLOSE, (
            f"STC_SHADOW_THRESHOLD({bot.STC_SHADOW_THRESHOLD}) > MAX_SECONDS_BEFORE_CLOSE({bot.MAX_SECONDS_BEFORE_CLOSE})")
        assert bot.STC_SHADOW_THRESHOLD >= bot.MIN_SECONDS_BEFORE_CLOSE, (
            f"STC_SHADOW_THRESHOLD({bot.STC_SHADOW_THRESHOLD}) < MIN_SECONDS_BEFORE_CLOSE({bot.MIN_SECONDS_BEFORE_CLOSE})")
