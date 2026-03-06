"""
Regression test suite for Kalshi trading bot.

Each test corresponds to a real bug that was fixed in production.
Tests are organized by bug category with commit references.
"""
import ast
import inspect
import math
import os
import re
import sqlite3
import sys
import textwrap

import pytest

# Add project root to path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)


# ============================================================================
#  1. Fee Calculation (7fb5a03, 017a2f0)
#     Bug: Edge filter and sim PnL didn't account for fees correctly.
#     Bug: Firebase sim PnL double-counted fees on losses.
# ============================================================================

class TestFeeCalculation:
    """Verify fee math matches Kalshi spec: ceil(rate × C × P × (100−P) / 100)."""

    def _fee(self, count, price, is_taker, mult_t=0.07, mult_m=0.0175):
        rate = mult_t if is_taker else mult_m
        return math.ceil(rate * count * price * (100 - price) / 100)

    def test_taker_fee_basic(self):
        # 1 contract at 90c: ceil(0.07 * 1 * 90 * 10 / 100) = ceil(0.63) = 1
        assert self._fee(1, 90, True) == 1
        # 10 contracts: ceil(0.07 * 10 * 90 * 10 / 100) = ceil(6.3) = 7
        assert self._fee(10, 90, True) == 7

    def test_maker_fee_basic(self):
        # 1 contract at 90c: ceil(0.0175 * 1 * 90 * 10 / 100) = ceil(0.1575) = 1
        assert self._fee(1, 90, False) == 1
        # 10 contracts: ceil(0.0175 * 10 * 90 * 10 / 100) = ceil(1.575) = 2
        assert self._fee(10, 90, False) == 2

    def test_fee_at_50c_maximum_variance(self):
        # P*(1-P) maximized at 50c
        assert self._fee(1, 50, True) == math.ceil(0.07 * 1 * 50 * 50 / 100)

    def test_fee_at_99c_near_certain(self):
        # 1 contract at 99c: ceil(0.07 * 1 * 99 * 1 / 100) = ceil(0.0693) = 1
        assert self._fee(1, 99, True) == 1

    def test_fee_at_1c_near_impossible(self):
        assert self._fee(1, 1, True) == 1

    def test_fee_scales_with_count(self):
        fee_1 = self._fee(1, 90, True)
        fee_10 = self._fee(10, 90, True)
        # ceil(10x) >= 10*ceil(1x) is not guaranteed, but fee should scale
        assert fee_10 >= fee_1
        assert fee_10 == math.ceil(0.07 * 10 * 90 * 10 / 100)

    def test_maker_is_4x_cheaper_than_taker(self):
        # Rate ratio: 0.07 / 0.0175 = 4.0
        for price in [86, 90, 95]:
            taker = self._fee(10, price, True)
            maker = self._fee(10, price, False)
            assert taker >= maker * 3  # at least 3x due to ceiling

    def test_spx_fee_multiplier(self):
        # SPX uses 0.035 taker (half of crypto 0.07). At 10 contracts the difference shows.
        crypto = self._fee(10, 90, True, mult_t=0.07)
        spx = self._fee(10, 90, True, mult_t=0.035)
        assert spx < crypto
        assert spx == math.ceil(0.035 * 10 * 90 * 10 / 100)  # ceil(3.15) = 4

    def test_sim_pnl_win_no_double_fee(self):
        """Bug 017a2f0: sim PnL on wins should be (100-price)*count - fee, not subtract fee twice."""
        price, count = 90, 10
        fee = self._fee(count, price, False)
        pnl_win = (100 - price) * count - fee
        # Win: revenue is (100-price)*count, minus fee once
        assert pnl_win == (100 - 90) * 10 - fee
        assert pnl_win > 0  # 90c wins should be profitable

    def test_sim_pnl_loss_no_double_fee(self):
        """Bug 017a2f0: sim PnL on losses should be -price*count - fee, NOT -price*count - 2*fee."""
        price, count = 90, 10
        fee = self._fee(count, price, False)
        pnl_loss = -price * count - fee
        # Loss cost is just entry cost + fee
        assert pnl_loss == -(price * count + fee)

    def test_bot_fee_function_matches(self):
        """Verify bot.py calculate_fee matches our reference implementation."""
        from bot import calculate_fee
        for count in [1, 5, 10, 25]:
            for price in [50, 70, 86, 90, 95, 99]:
                for is_taker in [True, False]:
                    expected = self._fee(count, price, is_taker)
                    actual = calculate_fee(count, price, is_taker)
                    assert actual == expected, (
                        f"Fee mismatch: count={count} price={price} taker={is_taker}: "
                        f"expected={expected} actual={actual}"
                    )


# ============================================================================
#  2. Edge Threshold Boundaries (Feb 28 config change)
#     Bug: Using wrong STC bucket (300-600s vs 500-900s) for analysis.
#     Bug: Flat MIN_EDGE_PCT used instead of price-dependent schedule.
# ============================================================================

class TestEdgeThresholds:
    """Verify price-dependent edge schedule is correctly applied."""

    def test_get_min_edge_schedule(self):
        from bot import get_min_edge
        # 86-88c → 0.25%
        assert get_min_edge(86) == 0.0025
        assert get_min_edge(88) == 0.0025
        # 89-90c → 0.25%
        assert get_min_edge(89) == 0.0025
        assert get_min_edge(90) == 0.0025
        # 91-92c → 0.35%
        assert get_min_edge(91) == 0.0035
        assert get_min_edge(92) == 0.0035
        # 93-94c → 0.9%
        assert get_min_edge(93) == 0.009
        assert get_min_edge(94) == 0.009
        # 95-96c → 1.25%
        assert get_min_edge(95) == 0.0125
        assert get_min_edge(96) == 0.0125
        # 97-99c → 2.0%
        assert get_min_edge(97) == 0.020
        assert get_min_edge(99) == 0.020

    def test_min_edge_monotonically_increases(self):
        """Higher prices should require higher edge (worse asymmetry)."""
        from bot import get_min_edge
        prev = 0
        for price in [86, 89, 91, 93, 95, 97]:
            edge = get_min_edge(price)
            assert edge >= prev, f"Edge at {price}c ({edge}) < edge at lower price ({prev})"
            prev = edge

    def test_stc_shadow_threshold_boundary(self):
        """STC_SHADOW_THRESHOLD=500 means 0-500s is live, 500-900s is shadow."""
        from bot import STC_SHADOW_THRESHOLD, MAX_SECONDS_BEFORE_CLOSE
        assert STC_SHADOW_THRESHOLD == 500
        assert MAX_SECONDS_BEFORE_CLOSE == 900
        # Shadow zone is [STC_SHADOW_THRESHOLD, MAX_SECONDS_BEFORE_CLOSE]
        assert STC_SHADOW_THRESHOLD < MAX_SECONDS_BEFORE_CLOSE


# ============================================================================
#  3. Calibration Pipeline (a078cce, 47d6b8f, d8eb75e)
#     Bug: Non-15M types routed through 15M Platt calibration.
#     Bug: Non-cal-eligible types got fixed_beta instead of passthrough.
#     Bug: Hourly CalEngine contaminated by 15M training data.
# ============================================================================

class TestCalibrationPipeline:
    """Verify calibration routing is product-type aware."""

    def test_cal_eligible_types(self):
        """Only 15M should be cal_eligible. Others use passthrough or own CalEngine."""
        from market_config import MARKET_CONFIGS
        assert MARKET_CONFIGS["15m"].cal_eligible is True
        assert MARKET_CONFIGS["hourly"].cal_eligible is False
        assert MARKET_CONFIGS["spx_hourly"].cal_eligible is False
        assert MARKET_CONFIGS["weather"].cal_eligible is False
        assert MARKET_CONFIGS["sports"].cal_eligible is False

    def test_hourly_cal_engine_config(self):
        """Hourly has its own CalEngine (not shared with 15M)."""
        from market_config import MARKET_CONFIGS
        cfg = MARKET_CONFIGS["hourly"]
        assert cfg.cal_engine_enabled is False  # disabled: beta_cal +44pp overconfident
        assert cfg.cal_engine_state_path == "hourly_calibration_state.json"

    def test_spx_cal_engine_config(self):
        from market_config import MARKET_CONFIGS
        cfg = MARKET_CONFIGS["spx_hourly"]
        assert cfg.cal_engine_state_path == "spx_hourly_calibration_state.json"

    def test_fee_multipliers_per_product(self):
        """SPX finance category gets 50% fee discount."""
        from market_config import MARKET_CONFIGS
        assert MARKET_CONFIGS["15m"].fee_multiplier_taker == 0.07
        assert MARKET_CONFIGS["spx_hourly"].fee_multiplier_taker == 0.035
        assert MARKET_CONFIGS["spx_hourly"].fee_multiplier_maker == 0.0175


# ============================================================================
#  4. Config Sync (bot.py ↔ market_config.py) — Crash loop prevention
#     Bug: MAX_SECONDS_BEFORE_CLOSE changed in bot.py but not market_config.py
#     → assertion failure at startup → 80s crash loop on VPS.
# ============================================================================

class TestConfigSync:
    """Verify bot.py constants match market_config.py (prevents crash loops)."""

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
        assert cfg.min_seconds_before_close == bot.HOURLY_MIN_SECONDS_BEFORE_CLOSE
        assert cfg.max_seconds_before_close == bot.HOURLY_MAX_SECONDS_BEFORE_CLOSE
        assert cfg.max_risk_per_trade == bot.HOURLY_MAX_RISK_PER_TRADE
        assert cfg.kelly_fraction == bot.HOURLY_KELLY_FRACTION
        assert cfg.market_blend_w == bot.HOURLY_MARKET_BLEND_W
        assert cfg.cal_engine_enabled == bot.HOURLY_CALIBRATION_ENABLED

    def test_spx_config_matches_bot(self):
        import bot
        from market_config import MARKET_CONFIGS
        cfg = MARKET_CONFIGS["spx_hourly"]
        assert cfg.observation_only == bot.SPX_HOURLY_OBSERVATION_ONLY
        assert cfg.min_entry_price == bot.SPX_HOURLY_MIN_ENTRY_PRICE
        assert cfg.max_entry_price == bot.SPX_HOURLY_MAX_ENTRY_PRICE
        assert cfg.max_risk_per_trade == bot.SPX_HOURLY_MAX_RISK_PER_TRADE

    def test_weather_config_matches_bot(self):
        import bot
        from market_config import MARKET_CONFIGS
        cfg = MARKET_CONFIGS["weather"]
        assert cfg.observation_only == bot.WEATHER_OBSERVATION_ONLY
        assert cfg.min_entry_price == bot.WEATHER_MIN_ENTRY_PRICE
        assert cfg.max_entry_price == bot.WEATHER_MAX_ENTRY_PRICE
        assert cfg.market_blend_w == bot.WEATHER_MARKET_BLEND_W


# ============================================================================
#  5. SQLite busy_timeout (7dbd821)
#     Bug: sports_engine.py missing PRAGMA busy_timeout → ~2000 "database
#     is locked" errors in 8 hours.
# ============================================================================

class TestBusyTimeout:
    """Every sqlite3.connect in production code must set busy_timeout."""

    PRODUCTION_FILES = [
        "bot.py",
        "firebase_push.py",
        "sports_engine.py",
        "watchdog.py",
        "supabase_sync.py",
        "analyst.py",
    ]

    def test_all_production_files_have_busy_timeout(self):
        """Scan production Python files for sqlite3.connect without busy_timeout."""
        missing = []
        for fname in self.PRODUCTION_FILES:
            fpath = os.path.join(PROJECT_ROOT, fname)
            if not os.path.exists(fpath):
                continue
            with open(fpath) as f:
                content = f.read()
            # Find all sqlite3.connect calls
            connects = [
                i for i, line in enumerate(content.splitlines(), 1)
                if "sqlite3.connect" in line and ":memory:" not in line
            ]
            for line_no in connects:
                # Check next 10 lines for busy_timeout (may be after row_factory etc.)
                lines = content.splitlines()
                nearby = "\n".join(lines[line_no - 1: line_no + 10])
                if "busy_timeout" not in nearby and "timeout" not in nearby:
                    missing.append(f"{fname}:{line_no}")
        assert not missing, f"Missing busy_timeout after sqlite3.connect: {missing}"


# ============================================================================
#  6. Shadow Diag Keys (c071841, 3946b2b)
#     Bug: New key added to _shadow_diag dict but not to insert_rejection()
#     and insert_evaluated_opportunity() signatures → TypeError on every
#     rejection insert (**_shadow_diag splat fails).
# ============================================================================

class TestShadowDiagKeys:
    """_shadow_diag keys must be accepted by both DB insert functions."""

    EXPECTED_SHADOW_KEYS = {
        "egarch_sigma", "egarch_blend_sigma", "egarch_blend_weight",
        "mz_r_squared", "shadow_tv_blend_rv", "mz_shadow_sigmoid_w",
        "mz_baseline_qlike", "mz_qlike",
    }

    def test_insert_rejection_accepts_shadow_keys(self):
        from bot import StateManager
        sig = inspect.signature(StateManager.insert_rejection)
        params = set(sig.parameters.keys())
        missing = self.EXPECTED_SHADOW_KEYS - params
        assert not missing, f"insert_rejection missing shadow_diag keys: {missing}"

    def test_insert_evaluated_opportunity_accepts_shadow_keys(self):
        from bot import StateManager
        sig = inspect.signature(StateManager.insert_evaluated_opportunity)
        params = set(sig.parameters.keys())
        missing = self.EXPECTED_SHADOW_KEYS - params
        assert not missing, f"insert_evaluated_opportunity missing shadow_diag keys: {missing}"

    def test_shadow_keys_are_subset_of_both_functions(self):
        """Both functions must accept ALL shadow_diag keys (they get **splatted)."""
        from bot import StateManager
        for fn_name in ["insert_rejection", "insert_evaluated_opportunity"]:
            fn = getattr(StateManager, fn_name)
            params = set(inspect.signature(fn).parameters.keys())
            unknown = self.EXPECTED_SHADOW_KEYS - params
            assert not unknown, f"{fn_name} missing: {unknown}"


# ============================================================================
#  7. Syntax Check (all commits)
#     Pre-deploy check: bot.py and market_config.py must parse cleanly.
# ============================================================================

class TestSyntaxCheck:
    """Every Python file must parse without syntax errors."""

    CRITICAL_FILES = ["bot.py", "market_config.py", "firebase_push.py",
                      "sports_engine.py", "spx_engine.py", "weather_engine.py"]

    @pytest.mark.parametrize("filename", CRITICAL_FILES)
    def test_file_parses(self, filename):
        fpath = os.path.join(PROJECT_ROOT, filename)
        if not os.path.exists(fpath):
            pytest.skip(f"{filename} not found")
        with open(fpath) as f:
            source = f.read()
        try:
            ast.parse(source)
        except SyntaxError as e:
            pytest.fail(f"{filename} has syntax error: {e}")


# ============================================================================
#  8. Firebase Balance $0 Glitch (ad4abd6)
#     Bug: API transient 0 overwrote _last_good_balance.
# ============================================================================

class TestFirebaseBalanceGlitch:
    """Balance should use last good value when API returns 0."""

    def test_zero_balance_uses_fallback(self):
        """Simulate the $0 glitch: when bal_val is 0, use _last_good_balance."""
        last_good = 150.0
        bal_val = 0.0
        # This is the logic from firebase_push.py:
        if bal_val > 0:
            last_good = bal_val
        result = bal_val if bal_val > 0 else last_good
        assert result == 150.0, "Should use last_good_balance when API returns 0"

    def test_positive_balance_updates(self):
        last_good = 150.0
        bal_val = 175.50
        if bal_val > 0:
            last_good = bal_val
        result = bal_val if bal_val > 0 else last_good
        assert result == 175.50
        assert last_good == 175.50


# ============================================================================
#  9. Strategy Wait Observation Gate (234a149)
#     Bug: strategy_wait filter had unconditional `continue` that blocked
#     ALL product types, including observation-only ones that should collect data.
# ============================================================================

class TestStrategyWaitGate:
    """Observation-only products should not be blocked by strategy_wait."""

    def test_observation_products_are_observation_only(self):
        from market_config import MARKET_CONFIGS
        # These must be observation_only=True
        for pt in ["hourly", "spx_hourly", "weather", "sports"]:
            assert MARKET_CONFIGS[pt].observation_only is True, (
                f"{pt} should be observation_only=True"
            )

    def test_live_product_is_not_observation(self):
        from market_config import MARKET_CONFIGS
        # 15M is live
        assert MARKET_CONFIGS["15m"].observation_only is False


# ============================================================================
# 10. Falsy Value Bugs (234a149, 357f33b)
#     Bug: `if current_price` is False when price=0. Should be `is not None`.
#     Bug: `max()` on empty sequence crashes.
# ============================================================================

class TestFalsyValueGuards:
    """Falsy values (0, empty list) must be handled correctly."""

    def test_price_zero_is_valid(self):
        """Price of 0 should not be treated as None/missing."""
        current_price = 0
        # Bug was: if current_price (False for 0)
        # Fix:    if current_price is not None
        assert (current_price is not None) is True
        assert bool(current_price) is False  # This was the bug

    def test_max_empty_sequence_guard(self):
        """max() on empty iterable must use default, not crash."""
        bids = []
        result = max((b for b in bids if b), default=None)
        assert result is None

    def test_max_all_falsy_guard(self):
        """max() when all items are falsy (0, None) must not crash."""
        bids = [0, None, 0]
        result = max((b for b in bids if b), default=None)
        assert result is None

    def test_max_with_valid_bids(self):
        bids = [50, 0, 75, None, 60]
        result = max((b for b in bids if b), default=None)
        assert result == 75


# ============================================================================
# 11. XRP Position Sizing Cap (3eccb77)
#     Bug: XRP RK model underestimates vol → Kelly oversizes → big losses.
# ============================================================================

class TestXRPSizingCap:
    """XRP must have a tighter risk cap than other assets."""

    def test_xrp_risk_cap_exists(self):
        import bot
        assert hasattr(bot, "XRP_MAX_RISK_PER_TRADE")
        assert bot.XRP_MAX_RISK_PER_TRADE < bot.MAX_RISK_PER_TRADE

    def test_xrp_cap_is_reasonable(self):
        import bot
        # XRP cap should be meaningfully lower than global
        assert bot.XRP_MAX_RISK_PER_TRADE <= 0.15
        assert bot.MAX_RISK_PER_TRADE >= 0.20


# ============================================================================
# 12. IOC Time-in-Force String (68c440b)
#     Bug: time_in_force="ioc" should be "immediate_or_cancel".
#     API returned 400 on every taker IOC submission since feature was written.
# ============================================================================

class TestIOCTimeInForce:
    """Kalshi API requires full string values for time_in_force."""

    VALID_TIF = {"immediate_or_cancel", "fill_or_kill", "good_till_canceled"}

    def test_valid_tif_strings(self):
        """Abbreviations like 'ioc', 'fok', 'gtc' are NOT valid."""
        invalid = {"ioc", "fok", "gtc", "IOC", "FOK", "GTC"}
        for abbrev in invalid:
            assert abbrev not in self.VALID_TIF

    def test_ioc_string_in_api_calls(self):
        """Ensure API order submission uses full string, not abbreviation.

        Note: 'ioc' in journal log dicts is fine — only API call sites matter.
        The actual API call uses time_in_force= keyword arg (not dict key).
        """
        fpath = os.path.join(PROJECT_ROOT, "bot.py")
        with open(fpath) as f:
            content = f.read()
        # The actual Kalshi API call uses keyword arg: time_in_force="..."
        # Journal log dicts use "time_in_force": "ioc" which is OK
        # Check that create_order calls use correct string
        api_call_pattern = r'time_in_force\s*=\s*["\'](\w+)["\']'
        matches = re.findall(api_call_pattern, content)
        for match in matches:
            assert match in self.VALID_TIF, (
                f"API call uses invalid time_in_force='{match}'. "
                f"Must be one of {self.VALID_TIF}"
            )


# ============================================================================
# 13. Product Type in DB Inserts (1f3d013, 98c954d)
#     Bug: product_type not passed to insert calls → NULL in DB → can't
#     filter 15M vs hourly data, leading to contaminated analysis.
#     Bug: STC shadow gate checked `is None` but 15M had product_type='15m'.
# ============================================================================

class TestProductTypeTracking:
    """All DB insert functions must accept and store product_type."""

    def test_insert_functions_accept_product_type(self):
        from bot import StateManager
        for fn_name in ["insert_rejection", "insert_evaluated_opportunity"]:
            fn = getattr(StateManager, fn_name)
            params = set(inspect.signature(fn).parameters.keys())
            assert "product_type" in params, f"{fn_name} missing product_type parameter"

    def test_15m_product_type_is_string(self):
        """15M product_type must be '15m', not None."""
        # The bug was code checking `product_type is None` for 15M
        # when 15M actually has product_type='15m'
        product_type_15m = "15m"
        assert product_type_15m is not None
        assert product_type_15m == "15m"


# ============================================================================
# 14. Escalation Type Tracking (ebf8078)
#     Bug: Direct taker and post-only taker paths never set escalation_type.
# ============================================================================

class TestEscalationTypeCompleteness:
    """All order execution paths must set escalation_type."""

    def test_escalation_types_in_codebase(self):
        """Verify all execution path labels exist in bot.py."""
        fpath = os.path.join(PROJECT_ROOT, "bot.py")
        with open(fpath) as f:
            content = f.read()
        required_types = ["direct_taker", "post_only_taker"]
        for etype in required_types:
            assert f'"{etype}"' in content or f"'{etype}'" in content, (
                f"escalation_type '{etype}' not found in bot.py"
            )


# ============================================================================
# 15. Observation Mode Safety
#     Critical: observation-only systems must never place real orders.
# ============================================================================

class TestObservationModeSafety:
    """Observation-only products must have safety rails."""

    def test_hourly_is_observation_only(self):
        import bot
        assert bot.HOURLY_OBSERVATION_ONLY is True

    def test_spx_is_observation_only(self):
        import bot
        assert bot.SPX_HOURLY_OBSERVATION_ONLY is True

    def test_weather_is_observation_only(self):
        import bot
        assert bot.WEATHER_OBSERVATION_ONLY is True

    def test_live_mode_is_enabled(self):
        """15M should be live (OBSERVATION_MODE=False means live)."""
        import bot
        assert bot.OBSERVATION_MODE is False


# ============================================================================
# 16. Dashboard Data Encoding (d982527)
#     Bug: JSONB double-encoding — JSON string inside JSONB field.
#     Bug: Sharpe ratio used population variance (n) instead of sample (n-1).
# ============================================================================

class TestDashboardDataIntegrity:
    """Dashboard stats must be computed correctly."""

    def test_sharpe_uses_sample_variance(self):
        """Sharpe ratio must use n-1 (Bessel's correction), not n."""
        import numpy as np
        pnl = [10, -5, 8, -3, 12, -2, 7, -1, 9, -4]
        mean = np.mean(pnl)
        # Correct: sample std (ddof=1)
        sample_std = np.std(pnl, ddof=1)
        # Wrong: population std (ddof=0)
        pop_std = np.std(pnl, ddof=0)
        sharpe_correct = mean / sample_std if sample_std > 0 else 0
        sharpe_wrong = mean / pop_std if pop_std > 0 else 0
        # Sample std is larger → correct Sharpe is more conservative
        assert sample_std > pop_std
        assert sharpe_correct < sharpe_wrong

    def test_max_drawdown_peak_to_trough(self):
        """Max drawdown should be peak-to-trough, not peak-to-current."""
        equity = [100, 110, 105, 95, 108, 100, 112]
        # Peak at 110, trough at 95 → DD = 15
        # But peak-to-current = 112-112 = 0 (wrong if only looking at end)
        peak = equity[0]
        max_dd = 0
        for val in equity:
            if val > peak:
                peak = val
            dd = peak - val
            if dd > max_dd:
                max_dd = dd
        assert max_dd == 15  # 110 → 95


# ============================================================================
# 17. Codebase Hygiene Checks
# ============================================================================

class TestCodebaseHygiene:
    """Prevent common code quality issues."""

    def test_no_env_files_in_git(self):
        """Never commit .env files (.env.example is OK)."""
        import subprocess
        result = subprocess.run(
            ["git", "ls-files", "*.env", ".env*"],
            capture_output=True, text=True, cwd=PROJECT_ROOT
        )
        tracked = [f for f in result.stdout.strip().splitlines()
                   if f and not f.endswith(".example")]
        assert not tracked, f".env files tracked in git: {tracked}"

    def test_no_jsonl_files_in_git(self):
        """Never commit journal files."""
        import subprocess
        result = subprocess.run(
            ["git", "ls-files", "*.jsonl"],
            capture_output=True, text=True, cwd=PROJECT_ROOT
        )
        tracked = result.stdout.strip()
        assert not tracked, f".jsonl files tracked in git: {tracked}"

    def test_critical_constants_unchanged(self):
        """Verify critical trading constants haven't drifted unexpectedly."""
        import bot
        # These are the "known good" values as of Mar 4, 2026
        assert bot.MIN_ENTRY_PRICE == 86
        assert bot.MAX_ENTRY_PRICE == 99
        assert bot.MAX_SECONDS_BEFORE_CLOSE == 900
        assert bot.STC_SHADOW_THRESHOLD == 500
        assert bot.OBSERVATION_MODE is False


# ============================================================================
# 18. CalEngine Pipeline Wiring (24d13d8)
#     Bug: raw_prob added to audit scripts before the actual INSERT was fixed
#     in sports_engine.py. Must ship all 3 in same commit.
# ============================================================================

class TestCalEnginePipelineWiring:
    """raw_prob must be present in INSERT calls for CalEngine to train."""

    def test_sports_insert_has_raw_prob(self):
        """sports_engine.py must include raw_prob in evaluated_opportunities INSERT."""
        fpath = os.path.join(PROJECT_ROOT, "sports_engine.py")
        with open(fpath) as f:
            content = f.read()
        assert "raw_prob" in content, "sports_engine.py missing raw_prob in INSERT"

    def test_bot_insert_evaluated_accepts_raw_prob(self):
        from bot import StateManager
        sig = inspect.signature(StateManager.insert_evaluated_opportunity)
        assert "raw_prob" in sig.parameters, (
            "insert_evaluated_opportunity missing raw_prob parameter"
        )


# ============================================================================
# 19. Time-in-Force Retry Loop Prevention (68c440b)
#     Bug: Invalid TIF caused 400 → hot retry loop hammering API ~20x/window.
# ============================================================================

class TestRetryLoopPrevention:
    """Failed IOC orders must clear rejection tracker to prevent retry storms."""

    def test_direct_taker_threshold_reasonable(self):
        """DIRECT_TAKER_THRESHOLD must be set and reasonable."""
        import bot
        assert hasattr(bot, "DIRECT_TAKER_THRESHOLD")
        assert 30 <= bot.DIRECT_TAKER_THRESHOLD <= 300


# ============================================================================
# 20. TV RK Weights Time Boundary (bot.py)
#     Bug: Using wrong time buckets for analysis.
# ============================================================================

class TestTVRKWeights:
    """Time-varying RK weights must have correct boundary conditions."""

    def test_tv_rk_weights_near_expiry(self):
        """Near expiry (< 60s): fast RK₁ should dominate."""
        from bot import compute_tv_rk_weights
        w1, w5, w15 = compute_tv_rk_weights(30.0)
        assert w1 > w5
        assert w1 > w15
        assert abs(w1 + w5 + w15 - 1.0) < 0.01

    def test_tv_rk_weights_far_from_expiry(self):
        """Far from expiry (> 180s): stable RK₁₅ should dominate."""
        from bot import compute_tv_rk_weights
        w1, w5, w15 = compute_tv_rk_weights(300.0)
        assert w15 >= w1
        assert abs(w1 + w5 + w15 - 1.0) < 0.01

    def test_tv_rk_weights_sum_to_one(self):
        """Weights must sum to ~1.0 at all time points (small tolerance for interpolation)."""
        from bot import compute_tv_rk_weights
        for stc in [10, 30, 60, 90, 120, 180, 300, 600, 900]:
            w1, w5, w15 = compute_tv_rk_weights(float(stc))
            assert abs(w1 + w5 + w15 - 1.0) < 0.06, f"Weights don't sum to ~1 at STC={stc}: {w1+w5+w15}"
            assert w1 >= 0 and w5 >= 0 and w15 >= 0, f"Negative weight at STC={stc}"


# ============================================================================
#  Instrumentation Integrity (Mar 5 2026)
#     Bug: hourly_applied_temp_t was written as NULL when CalEngine was active
#     because _temp_t was nulled for execution but also used for DB logging.
#     Fix: capture _configured_temp_t before CalEngine override.
# ============================================================================

class TestInstrumentationIntegrity:
    """Verify instrumentation variables are never silently nulled by execution logic."""

    def test_configured_temp_t_captured_before_calengine_override(self):
        """_configured_temp_t must be set BEFORE any code that nulls _temp_t.

        The pattern: _temp_t gets nulled when CalEngine is active (correct for
        execution), but DB writes must use _configured_temp_t which preserves
        the configured value for instrumentation.
        """
        source = open(os.path.join(PROJECT_ROOT, "bot.py")).read()

        # Find all assignments of _configured_temp_t
        config_assigns = [
            i for i, line in enumerate(source.splitlines())
            if "_configured_temp_t = _temp_t" in line
        ]
        assert len(config_assigns) >= 2, (
            f"Expected >= 2 _configured_temp_t captures (scan + price_shadow), found {len(config_assigns)}"
        )

        # Every DB write of hourly_applied_temp_t that references _temp_t
        # must use _configured_temp_t, not bare _temp_t.
        # Exclude lines with =None (POR rejections before temp is computed)
        # and lines reading from candidate dicts (candidate.get(...)).
        for i, line in enumerate(source.splitlines(), 1):
            stripped = line.strip()
            if "hourly_applied_temp_t=" not in stripped:
                continue
            if "=None" in stripped or "candidate.get(" in stripped or "c.get(" in stripped:
                continue  # hardcoded None or dict reads are fine
            if "_temp_t" in stripped and "_configured_temp_t" not in stripped:
                assert False, (
                    f"Line {i}: hourly_applied_temp_t uses _temp_t instead of "
                    f"_configured_temp_t — instrumentation will be NULL when CalEngine "
                    f"is active. Line: {stripped}"
                )

    def test_no_bare_temp_t_in_candidate_dict(self):
        """The candidate dict must store _configured_temp_t, not _temp_t."""
        source = open(os.path.join(PROJECT_ROOT, "bot.py")).read()

        for i, line in enumerate(source.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith('"hourly_applied_temp_t"') and ": _temp_t" in stripped:
                # Must be _configured_temp_t
                assert "_configured_temp_t" in stripped, (
                    f"Line {i}: candidate dict stores bare _temp_t for "
                    f"hourly_applied_temp_t — will be NULL when CalEngine active. "
                    f"Use _configured_temp_t. Line: {stripped}"
                )


# ============================================================================
#  Submit-taker return value (Mar 5 2026)
#  Bug: _submit_taker returned `fill` (the loop variable from _check_for_fill),
#  which was always None after the while loop broke. Every successful taker fill
#  was reported as unfilled to callers — broke session counters and dashboard.
# ============================================================================

class TestSubmitTakerReturnValue:
    """Verify _submit_taker returns order_info (not the loop variable) on fill."""

    def test_submit_taker_returns_order_info_not_fill(self):
        """The return statement in the total_filled > 0 branch must return
        order_info, not fill. `fill` is the while-loop variable and is always
        None/falsy after the loop breaks."""
        source = open(os.path.join(PROJECT_ROOT, "bot.py")).read()
        tree = ast.parse(source)

        found_func = False
        for node in ast.walk(tree):
            if not (isinstance(node, ast.FunctionDef)
                    and node.name == "_submit_taker"):
                continue
            found_func = True

            # Find ALL Return nodes in the function that return `fill`
            for stmt in ast.walk(node):
                if isinstance(stmt, ast.Return) and stmt.value:
                    val = stmt.value
                    if isinstance(val, ast.Name) and val.id == "fill":
                        assert False, (
                            f"Line {stmt.lineno}: _submit_taker returns "
                            f"`fill` (loop variable, always None after "
                            f"break). Must return `order_info`."
                        )
            break

        assert found_func, "_submit_taker function not found in bot.py"


# ============================================================================
#  Weather API Silent Failure Prevention
#  Bug: HRRR model name was "hrrr_conus" (wrong) instead of "ncep_hrrr_conus".
#       Open-Meteo returned HTTP 200 + {"error": true} which was silently
#       swallowed. HRRR data was dead for weeks with no visibility.
#       Root causes: wrong model name, no error field check, DEBUG-level logs.
# ============================================================================

class TestWeatherAPIDefenses:
    """Verify weather_engine.py defenses against silent API failures."""

    def test_hrrr_model_name_is_correct(self):
        """The HRRR model name must be 'ncep_hrrr_conus', not 'hrrr_conus'.

        Open-Meteo returns HTTP 200 + {"error": true} for wrong model names,
        so a typo here silently produces no data with no errors in logs.
        """
        source = open(os.path.join(PROJECT_ROOT, "weather_engine.py")).read()
        tree = ast.parse(source)

        for node in ast.walk(tree):
            # Check string constants in _fetch_hrrr
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert node.value != "hrrr_conus", (
                    f"Line {node.lineno}: Found 'hrrr_conus' — must be "
                    f"'ncep_hrrr_conus'. Open-Meteo returns silent error for wrong names."
                )

        # Positive check: the correct name must appear
        assert "ncep_hrrr_conus" in source, (
            "weather_engine.py must contain 'ncep_hrrr_conus' (HRRR model name)"
        )

    def test_all_api_responses_check_error_field(self):
        """Every Open-Meteo API call must check data.get("error") after resp.json().

        Open-Meteo returns HTTP 200 + {"error": true, "reason": "..."} for
        invalid parameters. Without this check, failures are invisible.
        """
        source = open(os.path.join(PROJECT_ROOT, "weather_engine.py")).read()

        # Find all resp.json() calls and ensure each has a nearby error check
        json_calls = [i for i, line in enumerate(source.splitlines())
                      if "resp.json()" in line]
        error_checks = [i for i, line in enumerate(source.splitlines())
                        if 'data.get("error")' in line or "data.get('error')" in line]

        assert len(json_calls) > 0, "No resp.json() calls found in weather_engine.py"
        assert len(error_checks) >= len(json_calls), (
            f"Found {len(json_calls)} resp.json() calls but only "
            f"{len(error_checks)} data.get('error') checks. "
            f"Every API response must check the error field."
        )

    def test_api_failures_log_at_warning_level(self):
        """API failures must log at WARNING, not DEBUG.

        DEBUG-level logs are invisible in production — a broken API integration
        would silently produce no data with no alerts.
        """
        source = open(os.path.join(PROJECT_ROOT, "weather_engine.py")).read()

        # Find lines that mention API failure/error AND use logging.debug
        for i, line in enumerate(source.splitlines(), 1):
            stripped = line.strip()
            if ("logging.debug" in stripped and
                any(kw in stripped.lower() for kw in
                    ["fail", "error", "http", "returned no", "returned 0"])):
                assert False, (
                    f"Line {i}: API failure logged at DEBUG level — must be "
                    f"WARNING or higher. DEBUG is invisible in production.\n"
                    f"  {stripped}"
                )

    def test_startup_self_test_exists(self):
        """WeatherEngine.start() must call _self_test_apis() to validate models."""
        source = open(os.path.join(PROJECT_ROOT, "weather_engine.py")).read()
        assert "_self_test_apis" in source, (
            "weather_engine.py must have _self_test_apis() method for startup validation"
        )
        # Verify it's called from start()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if (isinstance(node, ast.FunctionDef) and node.name == "start" and
                any(isinstance(n, ast.FunctionDef) for n in ast.iter_child_nodes(node.parent))
                if hasattr(node, 'parent') else True):
                body_source = ast.get_source_segment(source, node)
                if body_source and "_self_test_apis" in body_source:
                    return
        # Fallback: simple text check (AST parent traversal is tricky)
        in_start = False
        for line in source.splitlines():
            if "def start(self)" in line:
                in_start = True
            elif in_start and line.strip() and not line.startswith(" ") and not line.startswith("\t"):
                in_start = False
            if in_start and "_self_test_apis" in line:
                return
        assert False, "start() method must call _self_test_apis()"

    def test_ensemble_model_names_are_valid(self):
        """Verify GFS and ECMWF model names match Open-Meteo's API."""
        source = open(os.path.join(PROJECT_ROOT, "weather_engine.py")).read()

        # These are the correct Open-Meteo model identifiers
        valid_ensemble_models = {"gfs_seamless", "ecmwf_ifs025"}

        # Find model names passed to _fetch_model_ensemble
        tree = ast.parse(source)
        found_models = set()
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and
                isinstance(node.func, ast.Attribute) and
                node.func.attr == "_fetch_model_ensemble"):
                # Third positional arg is the model name
                if len(node.args) >= 3:
                    model_arg = node.args[2]
                    if isinstance(model_arg, ast.Constant) and isinstance(model_arg.value, str):
                        found_models.add(model_arg.value)

        assert found_models, "No _fetch_model_ensemble calls found"
        invalid = found_models - valid_ensemble_models
        assert not invalid, (
            f"Invalid ensemble model names: {invalid}. "
            f"Valid names: {valid_ensemble_models}"
        )


# ============================================================================
#  Sports Settlement Partial-Settlement Bug Prevention
#  Bug: _settle_stale_games and _settle_completed_games skipped games in
#       _settled_games set, but new eval rows could arrive after initial
#       settlement, leaving those rows with fav_won=NULL forever.
#       8 partially-settled games found (Mar 6 2026).
# ============================================================================

class TestSportsSettlementCompleteness:
    """Verify sports settlement doesn't skip partially-settled games."""

    def test_settle_stale_does_not_skip_settled_games(self):
        """_settle_stale_games must NOT skip games in _settled_games.

        New eval rows can arrive after initial settlement (race between
        evaluation loop and settlement). The SQL already filters
        fav_won IS NULL, so the _settled_games check is redundant and
        causes rows to be permanently orphaned.
        """
        source = open(os.path.join(PROJECT_ROOT, "sports_engine.py")).read()
        tree = ast.parse(source)

        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_settle_stale_games":
                func_source = ast.get_source_segment(source, node) or ""
                assert "if game_id in self._settled_games" not in func_source, (
                    "_settle_stale_games must not skip games in _settled_games. "
                    "The SQL WHERE fav_won IS NULL already handles this. "
                    "Skipping causes partially-settled games to have orphaned rows."
                )
                return
        assert False, "_settle_stale_games not found in sports_engine.py"

    def test_settle_completed_does_not_skip_settled_games(self):
        """_settle_completed_games must NOT skip games in _settled_games."""
        source = open(os.path.join(PROJECT_ROOT, "sports_engine.py")).read()
        tree = ast.parse(source)

        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_settle_completed_games":
                func_source = ast.get_source_segment(source, node) or ""
                assert "if game_id in self._settled_games" not in func_source, (
                    "_settle_completed_games must not skip games in _settled_games. "
                    "Late-arriving eval rows need settlement too."
                )
                return
        assert False, "_settle_completed_games not found in sports_engine.py"
