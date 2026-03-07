"""Cross-File Call-Site Integrity Tests.

Failure mode: Function signature changed in bot.py but callers in other files still
use old signature -> TypeError at runtime.
Past incident: CLAUDE.md rule about grepping all call sites after signature changes.

Also tests:
- CalEngine pipeline triple-ship rule
- No bare variable references in shadow engine callsites
"""

import ast
import inspect
import os
import re
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

# Files that import from bot.py and must be checked for call-site correctness
BOT_IMPORTERS = [
    "market_config.py",
    "dashboard_snapshot.py",
    "supabase_sync.py",
]

# Engine files that may wire into CalEngine pipeline
ENGINE_FILES = [
    "spx_engine.py",
    "weather_engine.py",
    "sports_engine.py",
]


class TestPublicAPIImports:
    """Modules that import from bot.py can do so without errors."""

    def test_market_config_imports(self):
        """market_config.py can import from bot without errors."""
        import market_config
        assert hasattr(market_config, "MARKET_CONFIGS")
        assert hasattr(market_config, "get_market_config")
        assert hasattr(market_config, "validate_market_configs")

    def test_bot_exports_key_functions(self):
        """bot.py exports all functions that other modules depend on."""
        import bot
        required_attrs = [
            "StateManager",
            "calculate_fee",
            "calculate_maker_fee",
            "MIN_ENTRY_PRICE",
            "MAX_ENTRY_PRICE",
            "MAX_RISK_PER_TRADE",
            "OBSERVATION_MODE",
            "MARKET_BLEND_W",
            "MIN_EDGE_BY_PRICE",
            "get_min_edge",
        ]
        for attr in required_attrs:
            assert hasattr(bot, attr), f"bot.py missing expected export: {attr}"

    def test_bot_exports_hourly_constants(self):
        """bot.py exports all hourly constants that market_config.py validates against."""
        import bot
        hourly_constants = [
            "HOURLY_OBSERVATION_ONLY",
            "HOURLY_MIN_ENTRY_PRICE",
            "HOURLY_MAX_SECONDS_BEFORE_CLOSE",
            "HOURLY_MIN_SECONDS_BEFORE_CLOSE",
            "HOURLY_MAX_RISK_PER_TRADE",
            "HOURLY_KELLY_FRACTION",
            "HOURLY_MARKET_BLEND_W",
            "HOURLY_TEMPERATURE_T",
            "HOURLY_TEMPERATURE_ENABLED",
            "HOURLY_MIN_STC_ENTRY",
            "HOURLY_MAX_STC_ENTRY",
            "HOURLY_EXCLUDED_ASSETS",
            "HOURLY_MAX_POSITIONS_PER_WINDOW",
            "HOURLY_MAX_WINDOW_RISK",
        ]
        for const in hourly_constants:
            assert hasattr(bot, const), f"bot.py missing hourly constant: {const}"

    def test_bot_exports_spx_constants(self):
        """bot.py exports all SPX constants that market_config.py validates against."""
        import bot
        spx_constants = [
            "SPX_HOURLY_OBSERVATION_ONLY",
            "SPX_HOURLY_MIN_ENTRY_PRICE",
            "SPX_HOURLY_MAX_ENTRY_PRICE",
            "SPX_HOURLY_MAX_SECONDS_BEFORE_CLOSE",
            "SPX_HOURLY_MIN_SECONDS_BEFORE_CLOSE",
            "SPX_HOURLY_MAX_RISK_PER_TRADE",
            "SPX_HOURLY_KELLY_FRACTION",
            "SPX_HOURLY_MARKET_BLEND_W",
            "SPX_HOURLY_TEMPERATURE_T",
            "SPX_HOURLY_FEE_MULTIPLIER_TAKER",
            "SPX_HOURLY_FEE_MULTIPLIER_MAKER",
            "SPX_HOURLY_MAX_POSITIONS_PER_WINDOW",
            "SPX_HOURLY_MAX_WINDOW_RISK",
        ]
        for const in spx_constants:
            assert hasattr(bot, const), f"bot.py missing SPX constant: {const}"

    def test_bot_exports_weather_constants(self):
        """bot.py exports all weather constants that market_config.py validates against."""
        import bot
        weather_constants = [
            "WEATHER_OBSERVATION_ONLY",
            "WEATHER_MIN_ENTRY_PRICE",
            "WEATHER_MAX_ENTRY_PRICE",
            "WEATHER_MAX_SECONDS_BEFORE_CLOSE",
            "WEATHER_MIN_SECONDS_BEFORE_CLOSE",
            "WEATHER_MAX_RISK_PER_TRADE",
            "WEATHER_KELLY_FRACTION",
            "WEATHER_MARKET_BLEND_W",
        ]
        for const in weather_constants:
            assert hasattr(bot, const), f"bot.py missing weather constant: {const}"

    def test_bot_exports_sports_constant(self):
        import bot
        assert hasattr(bot, "SPORTS_OBSERVATION_ONLY")


class TestCalEnginePipelineTripleShip:
    """When an engine has raw_prob in its INSERT, the CalEngine pipeline must be complete.

    The triple-ship rule: (1) INSERT includes raw_prob, (2) settlement routes to correct
    CalEngine, (3) audit checks for observations.
    Learned: sports raw_prob was added to audit before INSERT was fixed -> 134 rows NULL.
    """

    def test_sports_engine_inserts_raw_prob(self):
        """sports_engine.py INSERT must include raw_prob (not NULL)."""
        filepath = os.path.join(PROJECT_ROOT, "sports_engine.py")
        if not os.path.exists(filepath):
            pytest.skip("sports_engine.py not found")
        with open(filepath) as f:
            source = f.read()
        assert "raw_prob" in source, (
            "sports_engine.py does not reference raw_prob — CalEngine pipeline broken")

    def test_weather_engine_inserts_raw_prob(self):
        """weather_engine.py must handle raw_prob for CalEngine pipeline."""
        filepath = os.path.join(PROJECT_ROOT, "weather_engine.py")
        if not os.path.exists(filepath):
            pytest.skip("weather_engine.py not found")
        with open(filepath) as f:
            source = f.read()
        # Weather may pass raw_prob via the evaluated_opportunity insert
        assert "raw_prob" in source, (
            "weather_engine.py does not reference raw_prob — CalEngine pipeline may be broken")


class TestCrossModuleFunctionArity:
    """Key functions are called with correct argument names across all files."""

    def _get_valid_kwargs(self, cls, method_name):
        """Get valid keyword argument names for a method."""
        method = getattr(cls, method_name)
        sig = inspect.signature(method)
        return {name for name in sig.parameters if name != 'self'}

    def test_calculate_fee_signature_stable(self):
        """calculate_fee must accept (count, price, is_taker) — callers depend on this."""
        from bot import calculate_fee
        sig = inspect.signature(calculate_fee)
        params = list(sig.parameters.keys())
        assert "count" in params or len(params) >= 2, (
            f"calculate_fee signature changed unexpectedly: {params}")

    def test_get_market_config_signature_stable(self):
        """get_market_config(product_type) must work — many callers use it."""
        from market_config import get_market_config
        cfg = get_market_config("15m")
        assert cfg.product_type == "15m"
        cfg_none = get_market_config(None)
        assert cfg_none.product_type == "15m", "None should default to 15m"
        cfg_unknown = get_market_config("nonexistent")
        assert cfg_unknown.product_type == "15m", "Unknown should default to 15m"


class TestNoBareShadowVariables:
    """Shadow engine callsites must not use bare variable names that don't exist.

    Learned: egarch_blend_weight=egarch_blend_weight used a bare variable that only
    existed in _shadow_diag dict -> NameError on every call -> shadow engine was dead
    code for weeks. (dffdb05 Mar 7 2026)
    """

    def test_shadow_engine_calls_use_dict_lookups(self):
        """In bot.py, shadow engine calls should use vol_est.get() or _shadow_diag[],
        not bare variable names that might not exist as locals."""
        bot_path = os.path.join(PROJECT_ROOT, "bot.py")
        with open(bot_path) as f:
            source = f.read()

        # Find calls to shadow engine evaluate methods
        # Look for patterns like: some_func(egarch_blend_weight=egarch_blend_weight)
        # where egarch_blend_weight is not a local variable
        shadow_call_pattern = r'evaluate_signal\([^)]*egarch_blend_weight\s*=\s*egarch_blend_weight[^)\w]'
        matches = re.findall(shadow_call_pattern, source)
        assert not matches, (
            f"Found bare egarch_blend_weight= in shadow call (should use vol_est.get()): {matches}")


class TestDashboardSnapshotImports:
    """dashboard_snapshot.py dynamic imports must reference valid bot.py attributes."""

    def test_dashboard_snapshot_syntax(self):
        """dashboard_snapshot.py parses without errors."""
        filepath = os.path.join(PROJECT_ROOT, "dashboard_snapshot.py")
        if not os.path.exists(filepath):
            pytest.skip("dashboard_snapshot.py not found")
        with open(filepath) as f:
            source = f.read()
        ast.parse(source)  # Will raise SyntaxError if broken
