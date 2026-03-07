"""DB Signature Alignment Tests.

Failure mode: New key added to _shadow_diag without updating insert_rejection() /
insert_evaluated_opportunity() signatures -> runtime crash on first trade.
Past incident: Documented in CLAUDE.md as a critical rule.

Also checks:
- busy_timeout on all sqlite3.connect() calls
- check_same_thread=False on multi-threaded files
"""

import ast
import inspect
import os
import re
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

# Files that are called from multiple threads and MUST use check_same_thread=False
MULTI_THREAD_FILES = [
    "bot.py",
    "fifteenm_shadow.py",
    "hourly_alt_shadow.py",
    "spx_harrv_shadow.py",
    "supabase_sync.py",
]

# All production .py files (exclude venv, tests, scripts, migration utilities)
PRODUCTION_FILES = [
    "bot.py",
    "analyst.py",
    "dashboard_snapshot.py",
    "fifteenm_shadow.py",
    "hourly_alt_shadow.py",
    "market_config.py",
    "spx_engine.py",
    "spx_harrv_shadow.py",
    "sports_engine.py",
    "supabase_sync.py",
    "watchdog.py",
    "weather_engine.py",
    "capital_allocator.py",
]


class TestShadowDiagKeyCoverage:
    """Every _shadow_diag key must be accepted by both insert functions."""

    def _get_shadow_diag_keys(self):
        """Parse bot.py AST to find all keys in _shadow_diag = {...}."""
        bot_path = os.path.join(PROJECT_ROOT, "bot.py")
        with open(bot_path) as f:
            source = f.read()

        # Find _shadow_diag = { ... } via regex (AST won't easily find dict in function body)
        pattern = r'_shadow_diag\s*=\s*\{([^}]+)\}'
        match = re.search(pattern, source)
        assert match, "_shadow_diag dict not found in bot.py"

        # Extract keys from the dict literal
        dict_content = match.group(1)
        keys = re.findall(r'"(\w+)"', dict_content)
        assert len(keys) > 0, "_shadow_diag has no keys"
        return set(keys)

    def _get_function_params(self, func_name):
        """Get the parameter names of a function from bot.py's StateManager."""
        from bot import StateManager
        func = getattr(StateManager, func_name)
        sig = inspect.signature(func)
        # Skip 'self'
        return {name for name in sig.parameters if name != 'self'}

    def test_shadow_diag_keys_in_insert_rejection(self):
        """Every _shadow_diag key must be a parameter of insert_rejection()."""
        diag_keys = self._get_shadow_diag_keys()
        rejection_params = self._get_function_params("insert_rejection")
        missing = diag_keys - rejection_params
        assert not missing, (
            f"_shadow_diag keys missing from insert_rejection(): {missing}")

    def test_shadow_diag_keys_in_insert_evaluated_opportunity(self):
        """Every _shadow_diag key must be a parameter of insert_evaluated_opportunity()."""
        diag_keys = self._get_shadow_diag_keys()
        eval_params = self._get_function_params("insert_evaluated_opportunity")
        missing = diag_keys - eval_params
        assert not missing, (
            f"_shadow_diag keys missing from insert_evaluated_opportunity(): {missing}")


class TestBusyTimeout:
    """Every sqlite3.connect() must set PRAGMA busy_timeout."""

    def test_all_production_files_have_busy_timeout(self):
        """Every sqlite3.connect() call is followed by PRAGMA busy_timeout within 5 lines."""
        failures = []

        for filename in PRODUCTION_FILES:
            filepath = os.path.join(PROJECT_ROOT, filename)
            if not os.path.exists(filepath):
                continue

            with open(filepath) as f:
                lines = f.readlines()

            for i, line in enumerate(lines):
                if "sqlite3.connect(" in line:
                    # Check next 8 lines for busy_timeout
                    window = "".join(lines[i:i + 9])
                    if "busy_timeout" not in window and "timeout=" not in window:
                        failures.append(f"{filename}:{i+1}")

        assert not failures, (
            f"sqlite3.connect() without busy_timeout: {failures}")


class TestCheckSameThread:
    """Multi-threaded files must use check_same_thread=False."""

    def test_multi_thread_files_have_check_same_thread(self):
        """Files called from multiple threads must use check_same_thread=False."""
        failures = []

        for filename in MULTI_THREAD_FILES:
            filepath = os.path.join(PROJECT_ROOT, filename)
            if not os.path.exists(filepath):
                continue

            with open(filepath) as f:
                lines = f.readlines()

            for i, line in enumerate(lines):
                # Only match actual sqlite3.connect() calls, not comments
                stripped = line.lstrip()
                if stripped.startswith('#') or stripped.startswith('//'):
                    continue
                if 'sqlite3.connect(' not in line:
                    continue

                # Check the connect call and the next few lines for check_same_thread
                window = "".join(lines[i:i + 5])
                if "check_same_thread=False" not in window:
                    failures.append(f"{filename}:{i+1}")

        assert not failures, (
            f"Multi-threaded sqlite3.connect() without check_same_thread=False: {failures}")


class TestInsertFunctionSignatures:
    """Insert functions accept all columns they try to write."""

    def test_insert_rejection_has_product_type(self):
        """insert_rejection must accept product_type for multi-market support."""
        from bot import StateManager
        sig = inspect.signature(StateManager.insert_rejection)
        assert "product_type" in sig.parameters, (
            "insert_rejection missing product_type parameter")

    def test_insert_evaluated_opportunity_has_product_type(self):
        """insert_evaluated_opportunity must accept product_type."""
        from bot import StateManager
        sig = inspect.signature(StateManager.insert_evaluated_opportunity)
        assert "product_type" in sig.parameters, (
            "insert_evaluated_opportunity missing product_type parameter")

    def test_insert_evaluated_opportunity_has_raw_prob(self):
        """insert_evaluated_opportunity must accept raw_prob for CalEngine pipeline."""
        from bot import StateManager
        sig = inspect.signature(StateManager.insert_evaluated_opportunity)
        assert "raw_prob" in sig.parameters, (
            "insert_evaluated_opportunity missing raw_prob parameter")

    def test_insert_evaluated_opportunity_has_side(self):
        """insert_evaluated_opportunity must accept side for NO-side trading."""
        from bot import StateManager
        sig = inspect.signature(StateManager.insert_evaluated_opportunity)
        assert "side" in sig.parameters, (
            "insert_evaluated_opportunity missing side parameter")

    def test_insert_evaluated_opportunity_has_order_tracking(self):
        """insert_evaluated_opportunity must accept order tracking fields."""
        from bot import StateManager
        sig = inspect.signature(StateManager.insert_evaluated_opportunity)
        for field in ("order_id", "order_submitted_at", "order_outcome"):
            assert field in sig.parameters, (
                f"insert_evaluated_opportunity missing {field} parameter")

    def test_insert_evaluated_opportunity_has_weather_fields(self):
        """insert_evaluated_opportunity must accept weather ensemble fields."""
        from bot import StateManager
        sig = inspect.signature(StateManager.insert_evaluated_opportunity)
        weather_fields = [
            "wx_ensemble_mean", "wx_ensemble_std", "wx_bias_correction",
            "wx_n_members", "wx_market_type",
        ]
        for field in weather_fields:
            assert field in sig.parameters, (
                f"insert_evaluated_opportunity missing {field}")

    def test_insert_evaluated_opportunity_has_hourly_shadow_fields(self):
        """insert_evaluated_opportunity must accept hourly shadow comparison fields."""
        from bot import StateManager
        sig = inspect.signature(StateManager.insert_evaluated_opportunity)
        hourly_fields = [
            "hourly_pre_temp_prob", "hourly_applied_temp_t",
        ]
        for field in hourly_fields:
            assert field in sig.parameters, (
                f"insert_evaluated_opportunity missing {field}")


class TestSyntaxCheck:
    """All critical production files parse without syntax errors."""

    CRITICAL_FILES = [
        "bot.py",
        "market_config.py",
        "dashboard_snapshot.py",
        "analyst.py",
        "supabase_sync.py",
        "spx_engine.py",
        "weather_engine.py",
        "sports_engine.py",
        "fifteenm_shadow.py",
        "hourly_alt_shadow.py",
        "spx_harrv_shadow.py",
        "capital_allocator.py",
    ]

    def test_all_critical_files_parse(self):
        failures = []
        for filename in self.CRITICAL_FILES:
            filepath = os.path.join(PROJECT_ROOT, filename)
            if not os.path.exists(filepath):
                continue
            with open(filepath) as f:
                source = f.read()
            try:
                ast.parse(source)
            except SyntaxError as e:
                failures.append(f"{filename}: {e}")

        assert not failures, f"Syntax errors: {failures}"
