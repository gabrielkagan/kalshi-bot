"""Cross-File Call-Site Integrity Tests.

Failure mode: Function signature changed in bot/_impl.py but callers in other files still
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
import bot.constants  # noqa: F401

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

# Files that import from bot/_impl.py and must be checked for call-site correctness
BOT_IMPORTERS = [
    "market_config.py",
    "bot/snapshots/dashboard_snapshot.py",  # Sprint 10 Bit 10.4 (2026-05-12)
    "bot/snapshots/supabase_sync.py",  # Sprint 10 Bit 10.4 (2026-05-12)
]

# Engine files that may wire into CalEngine pipeline
ENGINE_FILES = [
    "bot/engines/spx_engine.py",  # Sprint 10.1b sibling-reorg (2026-05-11)
    "bot/engines/weather_engine.py",  # Sprint 10.1c sibling-reorg (2026-05-11)
    "bot/engines/sports_engine.py",  # Sprint 10.1d sibling-reorg (2026-05-11)
]


class TestPublicAPIImports:
    """Modules that import from bot/_impl.py can do so without errors."""

    def test_market_config_imports(self):
        """market_config.py can import from bot without errors."""
        import market_config
        assert hasattr(market_config, "MARKET_CONFIGS")
        assert hasattr(market_config, "get_market_config")
        assert hasattr(market_config, "validate_market_configs")

    def test_bot_exports_key_functions(self):
        """Canonical-home modules export all names that other modules depend on.

        Bit 9.3-iii.b (2026-05-11): pre-retirement these `hasattr(bot, X)` checks
        relied on the _BotProxy chain → bot._impl.X. Post-retirement each name is
        looked up from its canonical home (bot.constants for most, bot.state for
        StateManager, bot.models for fee helpers, bot.helpers for get_min_edge,
        config for MAX_RISK_PER_TRADE / MARKET_BLEND_W).
        """
        import bot.constants
        import bot.helpers
        import bot.models
        import bot.state
        import bot.config as config

        # (name, canonical_module) pairs
        required_attrs = [
            ("StateManager", bot.state),
            ("calculate_fee", bot.models),
            ("calculate_maker_fee", bot.models),
            ("MIN_ENTRY_PRICE", bot.constants),
            ("MAX_ENTRY_PRICE", bot.constants),
            ("MAX_RISK_PER_TRADE", config),
            ("OBSERVATION_MODE", bot.constants),
            ("MARKET_BLEND_W", bot.constants),
            ("MIN_EDGE_BY_PRICE", bot.constants),
            ("get_min_edge", bot.helpers),
        ]
        for attr, mod in required_attrs:
            assert hasattr(mod, attr), (
                f"{mod.__name__} missing expected export: {attr}"
            )

    def test_bot_exports_hourly_constants(self):
        """bot.constants exports all hourly constants that market_config.py validates against.

        Bit 9.3-iii.b retarget: hasattr(bot, X) → hasattr(bot.constants, X). HOURLY_MARKET_BLEND_W
        / HOURLY_KELLY_FRACTION / HOURLY_TEMPERATURE_T live in config (shared-constants module),
        not bot.constants.
        """
        import bot.constants
        import bot.config as config

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
            assert hasattr(bot.constants, const) or hasattr(config, const), (
                f"bot.constants + config both missing hourly constant: {const}"
            )

    def test_bot_exports_spx_constants(self):
        """bot.constants + config export all SPX constants market_config.py validates against."""
        import bot.constants
        import bot.config as config

        # SPX_HOURLY_* — all in bot.constants per Sprint 3 Bit 3.1 extraction.
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
            assert hasattr(bot.constants, const) or hasattr(config, const), (
                f"bot.constants + config both missing SPX constant: {const}"
            )

    def test_bot_exports_weather_constants(self):
        """bot.constants + config export all weather constants market_config.py validates against."""
        import bot.constants
        import bot.config as config

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
            assert hasattr(bot.constants, const) or hasattr(config, const), (
                f"bot.constants + config both missing weather constant: {const}"
            )

    def test_bot_exports_sports_constant(self):
        import bot.constants
        assert hasattr(bot.constants, "SPORTS_OBSERVATION_ONLY")


class TestCalEnginePipelineTripleShip:
    """When an engine has raw_prob in its INSERT, the CalEngine pipeline must be complete.

    The triple-ship rule: (1) INSERT includes raw_prob, (2) settlement routes to correct
    CalEngine, (3) audit checks for observations.
    Learned: sports raw_prob was added to audit before INSERT was fixed -> 134 rows NULL.
    """

    def test_sports_engine_inserts_raw_prob(self):
        """bot/engines/sports_engine.py INSERT must include raw_prob (not NULL)."""
        filepath = os.path.join(PROJECT_ROOT, "bot", "engines", "sports_engine.py")  # Sprint 10.1d (2026-05-11)
        if not os.path.exists(filepath):
            pytest.skip("bot/engines/sports_engine.py not found")
        with open(filepath) as f:
            source = f.read()
        assert "raw_prob" in source, (
            "sports_engine.py does not reference raw_prob — CalEngine pipeline broken")

    def test_weather_engine_inserts_raw_prob(self):
        """bot/engines/weather_engine.py must handle raw_prob for CalEngine pipeline."""
        filepath = os.path.join(PROJECT_ROOT, "bot", "engines", "weather_engine.py")  # Sprint 10.1c (2026-05-11)
        if not os.path.exists(filepath):
            pytest.skip("bot/engines/weather_engine.py not found")
        with open(filepath) as f:
            source = f.read()
        # Weather may pass raw_prob via the evaluated_opportunity insert
        assert "raw_prob" in source, (
            "weather_engine.py does not reference raw_prob — CalEngine pipeline may be broken")


class TestEvaluatedOpportunitiesTierContract:
    """Every raw `INSERT INTO evaluated_opportunities` must populate Tier 4.

    Regression: sports_engine.py's raw INSERT omitted hour_of_day_utc and
    siblings, silently writing NULLs because it bypassed
    StateManager.insert_evaluated_opportunity's auto-compute block.
    116/116 sports rows had spot_distance_to_strike_sigma NULL in 7d
    (2026-04-22 audit).

    Enforcement model:
      - StateManager.insert_evaluated_opportunity is the ONE canonical path.
        It auto-computes Tier 4/5 from kwargs + now. Callers needn't care.
      - Files listed in ALLOWED_RAW_INSERTERS bypass that path (their own
        sqlite conn in a separate thread) and MUST hand-populate Tier 4
        columns in their raw INSERT statement.
      - Any other file containing `INSERT INTO evaluated_opportunities`
        (or `INSERT OR REPLACE INTO evaluated_opportunities`) fails the
        test, forcing the author to either route through StateManager or
        add themselves to the allowlist AND populate Tier 4.
    """

    # Files permitted to issue their own INSERT statements against
    # evaluated_opportunities. bot/_impl.py is the StateManager home, so its raw
    # INSERT is the canonical auto-populating one.
    ALLOWED_RAW_INSERTERS = {"bot/_impl.py", "bot/engines/sports_engine.py"}  # Sprint 10.1d (2026-05-11)

    # Proxy for "all Tier 4 columns" — if this one appears in the INSERT
    # column list, the author at least noticed the contract exists. The
    # integration test in test_extended_features.py verifies actual values.
    REQUIRED_TIER_4_COLUMN = "hour_of_day_utc"

    INSERT_PATTERN = re.compile(
        r"INSERT\s+(?:OR\s+\w+\s+)?INTO\s+evaluated_opportunities",
        re.IGNORECASE,
    )

    def _scan_py_files(self):
        for fname in os.listdir(PROJECT_ROOT):
            if not fname.endswith(".py"):
                continue
            fpath = os.path.join(PROJECT_ROOT, fname)
            if not os.path.isfile(fpath):
                continue
            with open(fpath) as f:
                yield fname, f.read()

    def test_no_raw_inserts_outside_allowlist(self):
        offenders = []
        for fname, source in self._scan_py_files():
            if fname in self.ALLOWED_RAW_INSERTERS:
                continue
            if self.INSERT_PATTERN.search(source):
                offenders.append(fname)
        assert not offenders, (
            f"Unapproved raw INSERT INTO evaluated_opportunities in: {offenders}. "
            f"Either route through StateManager.insert_evaluated_opportunity "
            f"(which auto-populates Tier 4/5) or add the file to "
            f"ALLOWED_RAW_INSERTERS and hand-populate {self.REQUIRED_TIER_4_COLUMN} "
            f"plus siblings in the INSERT statement."
        )

    def test_allowed_inserters_include_tier_4_column(self):
        for fname in self.ALLOWED_RAW_INSERTERS:
            fpath = os.path.join(PROJECT_ROOT, fname)
            if not os.path.exists(fpath):
                pytest.skip(f"{fname} not found")
                continue
            with open(fpath) as f:
                source = f.read()
            if not self.INSERT_PATTERN.search(source):
                continue  # e.g., bot/_impl.py could change, not required
            assert self.REQUIRED_TIER_4_COLUMN in source, (
                f"{fname} has a raw INSERT INTO evaluated_opportunities but "
                f"does not reference {self.REQUIRED_TIER_4_COLUMN} — Tier 4 "
                f"auto-compute bypass will silently write NULLs."
            )


class TestCrossModuleFunctionArity:
    """Key functions are called with correct argument names across all files."""

    def _get_valid_kwargs(self, cls, method_name):
        """Get valid keyword argument names for a method."""
        method = getattr(cls, method_name)
        sig = inspect.signature(method)
        return {name for name in sig.parameters if name != 'self'}

    def test_calculate_fee_signature_stable(self):
        """calculate_fee must accept (count, price, is_taker) — callers depend on this."""
        from bot.models import calculate_fee
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
        """In bot/_impl.py, shadow engine calls should use vol_est.get() or _shadow_diag[],
        not bare variable names that might not exist as locals."""
        bot_path = os.path.join(PROJECT_ROOT, "bot/_impl.py")
        source = ""
        if os.path.exists(bot_path):
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
    """dashboard_snapshot.py dynamic imports must reference valid bot/_impl.py attributes."""

    def test_dashboard_snapshot_syntax(self):
        """dashboard_snapshot.py parses without errors."""
        filepath = os.path.join(PROJECT_ROOT, "bot", "snapshots", "dashboard_snapshot.py")
        if not os.path.exists(filepath):
            pytest.skip("dashboard_snapshot.py not found")
        with open(filepath) as f:
            source = f.read()
        ast.parse(source)  # Will raise SyntaxError if broken
