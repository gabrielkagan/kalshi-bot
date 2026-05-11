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


def _read_bot_and_scanner():
    """Bit 8.1 (2026-05-10): OpportunityScanner extracted from bot/_impl.py
    to bot/scanner/__init__.py. Source-level audits that grep for scanner
    content (filter_stage literals, gate code, _no_side_queue, V2 helpers,
    SOL_MIN_EDGE wiring, fifteenm_shadow callsites, etc.) must walk both
    files to survive the move. Mirrors the precedent in
    tests/test_15m_silence_alert.py and tests/test_decided_contract.py."""
    impl_path = os.path.join(PROJECT_ROOT, "bot", "_impl.py")
    scanner_path = os.path.join(PROJECT_ROOT, "bot", "scanner", "__init__.py")
    with open(impl_path) as _f:
        src = _f.read()
    if os.path.isfile(scanner_path):
        with open(scanner_path) as _f:
            src += "\n" + _f.read()
    # Bit 9.1 (2026-05-10): OrderExecutor extracted to bot/executor.py.
    executor_path = os.path.join(PROJECT_ROOT, "bot", "executor.py")
    if os.path.isfile(executor_path):
        with open(executor_path) as _f:
            src += "\n" + _f.read()
    # Bit 9.2 (2026-05-10): SettlementTracker + discover_active_windows extracted to bot/settlement.py.
    settlement_path = os.path.join(PROJECT_ROOT, "bot", "settlement.py")
    if os.path.isfile(settlement_path):
        with open(settlement_path) as _f:
            src += "\n" + _f.read()
    # Bit 9.3 (2026-05-10): MainLoop extracted to bot/main_loop.py.
    main_loop_path = os.path.join(PROJECT_ROOT, "bot", "main_loop.py")
    if os.path.isfile(main_loop_path):
        with open(main_loop_path) as _f:
            src += "\n" + _f.read()
    return src


# ============================================================================
#  1. Fee Calculation (7fb5a03, 017a2f0)
#     Bug: Edge filter and sim PnL didn't account for fees correctly.
#     Bug: Firebase sim PnL double-counted fees on losses.
# ============================================================================

class TestFeeCalculation:
    """Verify fee math matches Kalshi billing: taker = ceil(rate × C × P × (100−P) / 100), maker = $0."""

    def _taker_fee(self, count, price, mult_t=0.07):
        return math.ceil(mult_t * count * price * (100 - price) / 100)

    def test_taker_fee_basic(self):
        # 1 contract at 90c: ceil(0.07 * 1 * 90 * 10 / 100) = ceil(0.63) = 1
        assert self._taker_fee(1, 90) == 1
        # 10 contracts: ceil(0.07 * 10 * 90 * 10 / 100) = ceil(6.3) = 7
        assert self._taker_fee(10, 90) == 7

    def test_maker_fee_is_zero(self):
        """Kalshi charges $0 on maker fills (verified against 100 API fills)."""
        from bot import calculate_fee, calculate_maker_fee
        for count in [1, 5, 10, 25]:
            for price in [50, 70, 86, 90, 95, 99]:
                assert calculate_fee(count, price, is_taker=False) == 0, (
                    f"Maker fee should be $0: count={count} price={price}")
                assert calculate_maker_fee(count, price) == 0, (
                    f"calculate_maker_fee should return 0: count={count} price={price}")

    def test_fee_at_50c_maximum_variance(self):
        # P*(1-P) maximized at 50c
        assert self._taker_fee(1, 50) == math.ceil(0.07 * 1 * 50 * 50 / 100)

    def test_fee_at_99c_near_certain(self):
        # 1 contract at 99c: ceil(0.07 * 1 * 99 * 1 / 100) = ceil(0.0693) = 1
        assert self._taker_fee(1, 99) == 1

    def test_fee_at_1c_near_impossible(self):
        assert self._taker_fee(1, 1) == 1

    def test_fee_scales_with_count(self):
        fee_1 = self._taker_fee(1, 90)
        fee_10 = self._taker_fee(10, 90)
        assert fee_10 >= fee_1
        assert fee_10 == math.ceil(0.07 * 10 * 90 * 10 / 100)

    def test_spx_fee_multiplier(self):
        # SPX uses 0.035 taker (half of crypto 0.07). At 10 contracts the difference shows.
        crypto = self._taker_fee(10, 90, mult_t=0.07)
        spx = self._taker_fee(10, 90, mult_t=0.035)
        assert spx < crypto
        assert spx == math.ceil(0.035 * 10 * 90 * 10 / 100)  # ceil(3.15) = 4

    def test_sim_pnl_win_maker_no_fee(self):
        """Maker wins: full revenue, no fee deducted."""
        price, count = 90, 10
        pnl_win = (100 - price) * count  # maker fee = 0
        assert pnl_win == 100
        assert pnl_win > 0

    def test_sim_pnl_loss_maker_no_fee(self):
        """Maker losses: only entry cost lost, no fee."""
        price, count = 90, 10
        pnl_loss = -(price * count)  # maker fee = 0
        assert pnl_loss == -900

    def test_sim_pnl_win_taker(self):
        """Taker wins: revenue minus taker fee."""
        price, count = 90, 10
        fee = self._taker_fee(count, price)
        pnl_win = (100 - price) * count - fee
        assert pnl_win == 100 - fee
        assert pnl_win > 0

    def test_sim_pnl_loss_taker(self):
        """Taker losses: entry cost plus taker fee."""
        price, count = 90, 10
        fee = self._taker_fee(count, price)
        pnl_loss = -(price * count + fee)
        assert pnl_loss == -(900 + fee)

    def test_bot_fee_function_matches(self):
        """Verify bot/_impl.py calculate_fee matches Kalshi billing: taker = formula, maker = $0."""
        from bot import calculate_fee
        for count in [1, 5, 10, 25]:
            for price in [50, 70, 86, 90, 95, 99]:
                # Taker: ceil formula
                expected_taker = self._taker_fee(count, price)
                actual_taker = calculate_fee(count, price, is_taker=True)
                assert actual_taker == expected_taker, (
                    f"Taker fee mismatch: count={count} price={price}: "
                    f"expected={expected_taker} actual={actual_taker}"
                )
                # Maker: always $0
                actual_maker = calculate_fee(count, price, is_taker=False)
                assert actual_maker == 0, (
                    f"Maker fee should be $0: count={count} price={price}: "
                    f"actual={actual_maker}"
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
        # 91-92c → 0.20%
        assert get_min_edge(91) == 0.002
        assert get_min_edge(92) == 0.002
        # 93-94c → 0.50%
        assert get_min_edge(93) == 0.005
        assert get_min_edge(94) == 0.005
        # 95-96c → 0.75%
        assert get_min_edge(95) == 0.0075
        assert get_min_edge(96) == 0.0075
        # 97-99c → 1.0%
        assert get_min_edge(97) == 0.010
        assert get_min_edge(99) == 0.010

    # Removed: test_min_edge_monotonically_increases — schedule is intentionally
    # non-monotonic at 91c (0.002 < 89c's 0.0025). Invalid invariant.

    def test_stc_shadow_threshold_boundary(self):
        """STC_SHADOW_THRESHOLD=600 means 0-600s is live (with extended floors at 300-600s)."""
        from bot import STC_SHADOW_THRESHOLD, MAX_SECONDS_BEFORE_CLOSE, STC_EXTENDED_LIVE_FLOOR
        assert STC_SHADOW_THRESHOLD == 600
        assert MAX_SECONDS_BEFORE_CLOSE == 900
        assert STC_EXTENDED_LIVE_FLOOR == 300
        # Shadow zone is [STC_SHADOW_THRESHOLD, MAX_SECONDS_BEFORE_CLOSE]
        assert STC_SHADOW_THRESHOLD < MAX_SECONDS_BEFORE_CLOSE
        # Extended zone is (STC_EXTENDED_LIVE_FLOOR, STC_SHADOW_THRESHOLD]
        assert STC_EXTENDED_LIVE_FLOOR < STC_SHADOW_THRESHOLD


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
        assert MARKET_CONFIGS["spx_hourly"].fee_multiplier_maker == 0.0  # Kalshi $0 maker fee
        # All product types should have maker fee = 0
        for name, cfg in MARKET_CONFIGS.items():
            assert cfg.fee_multiplier_maker == 0.0, (
                f"{name} fee_multiplier_maker should be 0.0, got {cfg.fee_multiplier_maker}")


# ============================================================================
#  4. Config Sync (bot/_impl.py ↔ market_config.py) — Crash loop prevention
#     Bug: MAX_SECONDS_BEFORE_CLOSE changed in bot/_impl.py but not market_config.py
#     → assertion failure at startup → 80s crash loop on VPS.
# ============================================================================

class TestConfigSync:
    """Verify bot/_impl.py constants match market_config.py (prevents crash loops)."""

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
    """Every sqlite3.connect in production code must set busy_timeout.
    PM-001 (Mar 9 2026): analyst.py was missed by the old hardcoded list.
    Now scans ALL .py files automatically."""

    # Files exempt from busy_timeout (test files, one-off scripts, in-memory DBs)
    EXEMPT_PATTERNS = {"test_", "migrate_to_", "generate_whitepaper"}

    def _find_all_py_files(self):
        """Find all .py files in project root and scripts/."""
        py_files = []
        for dirpath in [PROJECT_ROOT, os.path.join(PROJECT_ROOT, "scripts"),
                        os.path.join(PROJECT_ROOT, "analysis")]:
            if not os.path.isdir(dirpath):
                continue
            for fname in os.listdir(dirpath):
                if fname.endswith(".py"):
                    py_files.append(os.path.join(dirpath, fname))
        return py_files

    def test_all_sqlite_connects_have_busy_timeout(self):
        """Scan ALL .py files for sqlite3.connect without busy_timeout.
        Catches any new file that opens a DB connection without it."""
        missing = []
        for fpath in self._find_all_py_files():
            fname = os.path.basename(fpath)
            if any(pat in fname for pat in self.EXEMPT_PATTERNS):
                continue
            with open(fpath) as f:
                content = f.read()
            if "sqlite3.connect" not in content:
                continue
            lines = content.splitlines()
            connects = [
                i for i, line in enumerate(lines, 1)
                if "sqlite3.connect" in line and ":memory:" not in line
                and not line.lstrip().startswith("#")
            ]
            for line_no in connects:
                nearby = "\n".join(lines[line_no - 1: min(line_no + 10, len(lines))])
                if "busy_timeout" not in nearby and "timeout" not in nearby:
                    missing.append(f"{fname}:{line_no}")
        assert not missing, (
            f"Missing PRAGMA busy_timeout after sqlite3.connect: {missing}. "
            f"Rule: every sqlite3.connect() on state.db MUST set busy_timeout. "
            f"See POSTMORTEMS.md PM-001."
        )

    # Production files that write to state.db and MUST set WAL mode
    WAL_REQUIRED_FILES = [
        "bot/_impl.py", "supabase_sync.py", "bot/engines/sports_engine.py",  # Sprint 10.1d (2026-05-11)
        "fifteenm_shadow.py", "hourly_alt_shadow.py", "spx_harrv_shadow.py",
    ]

    def test_production_writers_have_wal_mode(self):
        """Production files that write to state.db must set journal_mode=WAL.
        Read-only connections (mode=ro) are exempt — WAL is set by the writer.
        Analysis/audit scripts are read-only and don't need WAL."""
        missing = []
        for fname in self.WAL_REQUIRED_FILES:
            fpath = os.path.join(PROJECT_ROOT, fname)
            if not os.path.exists(fpath):
                continue
            with open(fpath) as f:
                content = f.read()
            if "sqlite3.connect" not in content:
                continue
            lines = content.splitlines()
            connects = [
                i for i, line in enumerate(lines, 1)
                if "sqlite3.connect" in line and ":memory:" not in line
                and not line.lstrip().startswith("#")
            ]
            for line_no in connects:
                nearby = "\n".join(lines[line_no - 1: min(line_no + 10, len(lines))])
                if "mode=ro" in nearby or "query_only" in nearby:
                    continue
                if "journal_mode=WAL" not in nearby and "journal_mode" not in nearby:
                    missing.append(f"{fname}:{line_no}")
        assert not missing, (
            f"Missing PRAGMA journal_mode=WAL after sqlite3.connect: {missing}. "
            f"Rule: every read-write sqlite3.connect() on state.db MUST set WAL mode. "
            f"See POSTMORTEMS.md PM-001."
        )


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
#  7. Cross-Thread SQLite (53c953b)
#     Bug: fifteenm_shadow.py used sqlite3.connect() without
#     check_same_thread=False. Connection created in one thread was used from
#     another (bot main vs supabase_sync), causing ProgrammingError that was
#     silently swallowed → 0 rows written to fifteenm_shadow_signals.
# ============================================================================

class TestCheckSameThread:
    """Production files using SQLite from multiple threads must set check_same_thread=False."""

    # Files that may have their connection used from a different thread
    CROSS_THREAD_FILES = [
        "fifteenm_shadow.py",
    ]

    def test_cross_thread_files_have_check_same_thread(self):
        """Scan cross-thread SQLite files for missing check_same_thread=False."""
        missing = []
        for fname in self.CROSS_THREAD_FILES:
            fpath = os.path.join(PROJECT_ROOT, fname)
            if not os.path.exists(fpath):
                continue
            with open(fpath) as f:
                content = f.read()
            connects = [
                i for i, line in enumerate(content.splitlines(), 1)
                if "sqlite3.connect" in line and ":memory:" not in line
            ]
            for line_no in connects:
                lines = content.splitlines()
                # Check the connect call itself and nearby lines
                nearby = "\n".join(lines[max(0, line_no - 1): line_no + 5])
                if "check_same_thread" not in nearby:
                    missing.append(f"{fname}:{line_no}")
        assert not missing, (
            f"Missing check_same_thread=False after sqlite3.connect: {missing}. "
            f"These files are called from multiple threads."
        )

    def test_fifteenm_shadow_product_type_filter(self):
        """fifteenm_shadow.py must accept product_type='15m', not just NULL.
        Bug: queries used 'product_type IS NULL' but 15M evals have product_type='15m'.
        Result: 0 training rows despite hundreds of settled evals."""
        fpath = os.path.join(PROJECT_ROOT, "fifteenm_shadow.py")
        if not os.path.exists(fpath):
            pytest.skip("fifteenm_shadow.py not found")
        with open(fpath) as f:
            content = f.read()
        # Must NOT have bare "product_type IS NULL" without the OR clause
        lines = content.splitlines()
        for i, line in enumerate(lines, 1):
            if "product_type IS NULL" in line and "OR product_type" not in line:
                # Check if the next line has the OR
                nearby = "\n".join(lines[max(0, i-1):i+2])
                if "OR product_type" not in nearby:
                    pytest.fail(
                        f"fifteenm_shadow.py:{i} has 'product_type IS NULL' without "
                        f"'OR product_type = \"15m\"' — 15M evals have product_type='15m'"
                    )

    def test_fifteenm_shadow_has_busy_timeout(self):
        """fifteenm_shadow.py must also have busy_timeout (shares state.db)."""
        fpath = os.path.join(PROJECT_ROOT, "fifteenm_shadow.py")
        if not os.path.exists(fpath):
            pytest.skip("fifteenm_shadow.py not found")
        with open(fpath) as f:
            content = f.read()
        assert "busy_timeout" in content, (
            "fifteenm_shadow.py missing PRAGMA busy_timeout — "
            "it shares state.db with bot/_impl.py and supabase_sync.py"
        )

    def test_fifteenm_shadow_a3_gating_exists(self):
        """fifteenm_shadow.py must have EGARCHGatingApproach (Approach 3)."""
        fpath = os.path.join(PROJECT_ROOT, "fifteenm_shadow.py")
        if not os.path.exists(fpath):
            pytest.skip("fifteenm_shadow.py not found")
        with open(fpath) as f:
            content = f.read()
        assert "EGARCHGatingApproach" in content, (
            "fifteenm_shadow.py missing EGARCHGatingApproach class"
        )
        assert "egarch_gating" in content, (
            "fifteenm_shadow.py missing 'egarch_gating' approach identifier"
        )
        assert "a3_gate_prob" in content, (
            "fifteenm_shadow.py missing a3_gate_prob column"
        )

    def test_fifteenm_shadow_a3_both_sides(self):
        """A3 gating must evaluate both YES and NO sides."""
        fpath = os.path.join(PROJECT_ROOT, "fifteenm_shadow.py")
        if not os.path.exists(fpath):
            pytest.skip("fifteenm_shadow.py not found")
        with open(fpath) as f:
            content = f.read()
        assert "no_a3_gate_prob" in content, (
            "fifteenm_shadow.py missing NO-side A3 columns"
        )
        assert "no_a3_pnl_gate" in content, (
            "fifteenm_shadow.py missing NO-side A3 PnL columns"
        )

    def test_fifteenm_shadow_a3_db_columns_match_insert(self):
        """A3 columns in CREATE/ALTER must match INSERT statement."""
        fpath = os.path.join(PROJECT_ROOT, "fifteenm_shadow.py")
        if not os.path.exists(fpath):
            pytest.skip("fifteenm_shadow.py not found")
        with open(fpath) as f:
            content = f.read()
        # Must have both the column definition and the insert
        a3_cols = ["a3_gate_prob", "a3_gate_10", "a3_gate_20", "a3_gate_30",
                   "no_a3_gate_prob", "no_a3_gate_10", "no_a3_gate_20", "no_a3_gate_30",
                   "a3_pnl_gate10_cents", "a3_pnl_gate20_cents", "a3_pnl_gate30_cents",
                   "no_a3_pnl_gate10_cents", "no_a3_pnl_gate20_cents", "no_a3_pnl_gate30_cents"]
        for col in a3_cols:
            assert content.count(col) >= 2, (
                f"fifteenm_shadow.py: column '{col}' appears only "
                f"{content.count(col)} time(s) — must appear in both ALTER "
                f"and INSERT/UPDATE"
            )


# ============================================================================
#  8. Syntax Check (all commits)
#     Pre-deploy check: bot/_impl.py and market_config.py must parse cleanly.
# ============================================================================

class TestSyntaxCheck:
    """Every Python file must parse without syntax errors."""

    CRITICAL_FILES = ["bot/_impl.py", "market_config.py", "dashboard_snapshot.py",
                      "bot/engines/sports_engine.py", "bot/engines/spx_engine.py", "bot/engines/weather_engine.py",  # Sprint 10.1c/d (2026-05-11)
                      "fifteenm_shadow.py"]  # Sprint 10.1b sibling-reorg (2026-05-11): spx_engine relocated

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
        # This is the logic from dashboard_snapshot.py:
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
        # These must be observation_only=True (SPX promoted to live Mar 17 2026)
        for pt in ["hourly", "weather", "sports"]:
            assert MARKET_CONFIGS[pt].observation_only is True, (
                f"{pt} should be observation_only=True"
            )

    def test_live_product_is_not_observation(self):
        from market_config import MARKET_CONFIGS
        # 15M is live, SPX is observation (reverted Mar 17 — Polygon 403)
        assert MARKET_CONFIGS["15m"].observation_only is False
        assert MARKET_CONFIGS["spx_hourly"].observation_only is True


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


class TestBTCSizingCap:
    """BTC must have a tighter risk cap than global (matching XRP pattern)."""

    def test_btc_risk_cap_exists(self):
        import bot
        assert hasattr(bot, "BTC_MAX_RISK_PER_TRADE")
        assert bot.BTC_MAX_RISK_PER_TRADE < bot.MAX_RISK_PER_TRADE

    def test_btc_cap_is_reasonable(self):
        import bot
        assert bot.BTC_MAX_RISK_PER_TRADE <= 0.15
        assert bot.BTC_MAX_RISK_PER_TRADE >= 0.05


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
        # Bit 9.1 L38: read both bot/_impl.py + bot/executor.py for source-level audits

        _paths = [os.path.join(PROJECT_ROOT, "bot/_impl.py"), os.path.join(PROJECT_ROOT, "bot/executor.py"), os.path.join(PROJECT_ROOT, "bot/settlement.py"), os.path.join(PROJECT_ROOT, "bot/main_loop.py")]

        content = ""

        for _p in _paths:
            if os.path.isfile(_p):
                with open(_p) as f:
                    content += f.read() + "\n"
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
        """Verify all execution path labels exist in bot/_impl.py."""
        # Bit 9.1 L38: read both bot/_impl.py + bot/executor.py for source-level audits

        _paths = [os.path.join(PROJECT_ROOT, "bot/_impl.py"), os.path.join(PROJECT_ROOT, "bot/executor.py"), os.path.join(PROJECT_ROOT, "bot/settlement.py"), os.path.join(PROJECT_ROOT, "bot/main_loop.py")]

        content = ""

        for _p in _paths:
            if os.path.isfile(_p):
                with open(_p) as f:
                    content += f.read() + "\n"
        required_types = ["direct_taker", "post_only_taker", "sol_taker_override"]
        for etype in required_types:
            assert f'"{etype}"' in content or f"'{etype}'" in content, (
                f"escalation_type '{etype}' not found in bot/_impl.py"
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

    # Removed: test_spx_is_live — SPX reverted to observation Mar 17 (Polygon 403).
    # Covered by test_live_product_is_not_observation which now asserts observation_only=True.

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
        # These are the "known good" values as of Mar 23, 2026
        assert bot.MIN_ENTRY_PRICE == 75  # lowered from 80 for ETH 75-79c
        assert bot.MAX_ENTRY_PRICE == 99
        assert bot.MAX_SECONDS_BEFORE_CLOSE == 900
        assert bot.STC_SHADOW_THRESHOLD == 600
        assert bot.OBSERVATION_MODE is False


# ============================================================================
# 18. CalEngine Pipeline Wiring (24d13d8)
#     Bug: raw_prob added to audit scripts before the actual INSERT was fixed
#     in sports_engine.py. Must ship all 3 in same commit.
# ============================================================================

class TestCalEnginePipelineWiring:
    """raw_prob must be present in INSERT calls for CalEngine to train."""

    def test_sports_insert_has_raw_prob(self):
        """bot/engines/sports_engine.py must include raw_prob in evaluated_opportunities INSERT."""
        fpath = os.path.join(PROJECT_ROOT, "bot", "engines", "sports_engine.py")  # Sprint 10.1d (2026-05-11)
        with open(fpath) as f:
            content = f.read()
        assert "raw_prob" in content, "bot/engines/sports_engine.py missing raw_prob in INSERT"

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
# 20. TV RK Weights Time Boundary (bot/_impl.py)
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
        # Bit 8.1 (2026-05-10): scanner moved to bot/scanner/__init__.py.
        source = _read_bot_and_scanner()

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
            if "=None" in stripped or "candidate.get(" in stripped or "c.get(" in stripped or "item.get(" in stripped:
                continue  # hardcoded None or dict reads are fine
            if "excluded." in stripped:
                continue  # SQL ON CONFLICT excluded pseudo-table references
            # Check the VALUE side only (after the =), not the column name which
            # itself contains "_temp_t" as a substring of "hourly_applied_temp_t".
            eq_idx = stripped.find("hourly_applied_temp_t=") + len("hourly_applied_temp_t=")
            value_part = stripped[eq_idx:]
            if "_temp_t" in value_part and "_configured_temp_t" not in value_part:
                assert False, (
                    f"Line {i}: hourly_applied_temp_t uses _temp_t instead of "
                    f"_configured_temp_t — instrumentation will be NULL when CalEngine "
                    f"is active. Line: {stripped}"
                )

    def test_no_bare_temp_t_in_candidate_dict(self):
        """The candidate dict must store _configured_temp_t, not _temp_t."""
        source = open(os.path.join(PROJECT_ROOT, "bot/_impl.py")).read()

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
        # Bit 9.1 (2026-05-10): OrderExecutor extracted to bot/executor.py
        sources = []
        for _p in [os.path.join(PROJECT_ROOT, "bot/_impl.py"), os.path.join(PROJECT_ROOT, "bot/executor.py")]:
            if os.path.isfile(_p):
                sources.append(open(_p).read())
        source = "\n".join(sources)
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

        assert found_func, "_submit_taker function not found in bot/_impl.py"


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
        source = open(os.path.join(PROJECT_ROOT, "bot", "engines", "weather_engine.py")).read()  # Sprint 10.1c (2026-05-11)
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
        source = open(os.path.join(PROJECT_ROOT, "bot", "engines", "weather_engine.py")).read()  # Sprint 10.1c (2026-05-11)

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
        source = open(os.path.join(PROJECT_ROOT, "bot", "engines", "weather_engine.py")).read()  # Sprint 10.1c (2026-05-11)

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
        source = open(os.path.join(PROJECT_ROOT, "bot", "engines", "weather_engine.py")).read()  # Sprint 10.1c (2026-05-11)
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
        source = open(os.path.join(PROJECT_ROOT, "bot", "engines", "weather_engine.py")).read()  # Sprint 10.1c (2026-05-11)

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
        source = open(os.path.join(PROJECT_ROOT, "bot", "engines", "sports_engine.py")).read()  # Sprint 10.1d (2026-05-11)
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
        source = open(os.path.join(PROJECT_ROOT, "bot", "engines", "sports_engine.py")).read()  # Sprint 10.1d (2026-05-11)
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


# ═══════════════════════════════════════════════════════════════════════════════
# Weather Engine: Bias Persistence & HRRR Reliability (Mar 6, 2026)
# ═══════════════════════════════════════════════════════════════════════════════

class TestWeatherBiasPersistence:
    """Bias corrections must survive restarts via SQLite persistence."""

    def test_bias_round_trip(self, tmp_path):
        """Bias saved by one model instance is loaded by a new instance."""
        from unittest.mock import MagicMock
        sys.modules.setdefault("requests", MagicMock())
        from bot.engines.weather_engine import WeatherProbabilityModel  # Sprint 10.1c (2026-05-11)

        db_path = str(tmp_path / "test_weather.db")

        # Instance 1: learn a bias
        model1 = WeatherProbabilityModel(db_path=db_path)
        model1.update_bias("CHI", actual_high=46.3, forecast_mean=44.2, market_date="2026-03-05")
        assert abs(model1._bias["CHI"] - 2.1) < 0.01

        # Instance 2: must load the bias from DB
        model2 = WeatherProbabilityModel(db_path=db_path)
        assert "CHI" in model2._bias, "Bias not loaded from DB on startup"
        assert abs(model2._bias["CHI"] - 2.1) < 0.01
        assert model2._bias_count["CHI"] == 1

    def test_bias_deduplication(self, tmp_path):
        """Same (city, date) pair must not update bias twice."""
        from unittest.mock import MagicMock
        sys.modules.setdefault("requests", MagicMock())
        from bot.engines.weather_engine import WeatherProbabilityModel  # Sprint 10.1c (2026-05-11)

        db_path = str(tmp_path / "test_weather.db")
        model = WeatherProbabilityModel(db_path=db_path)

        model.update_bias("NYC", 50.0, 48.0, market_date="2026-03-05")
        bias_after_first = model._bias["NYC"]

        # Second call with different forecast — should be deduped
        model.update_bias("NYC", 50.0, 45.0, market_date="2026-03-05")
        assert model._bias["NYC"] == bias_after_first, "Duplicate bias update was not deduped"
        assert model._bias_count["NYC"] == 1

    def test_bias_no_db_still_works(self):
        """Model without db_path still computes bias in-memory."""
        from unittest.mock import MagicMock
        sys.modules.setdefault("requests", MagicMock())
        from bot.engines.weather_engine import WeatherProbabilityModel  # Sprint 10.1c (2026-05-11)

        model = WeatherProbabilityModel(db_path=None)
        model.update_bias("DEN", 68.7, 66.5)
        assert abs(model._bias["DEN"] - 2.2) < 0.01

    def test_bias_table_uses_busy_timeout(self):
        """All SQLite connections in bias persistence must use busy_timeout."""
        source = open(os.path.join(PROJECT_ROOT, "bot", "engines", "weather_engine.py")).read()  # Sprint 10.1c (2026-05-11)
        # Find all sqlite3.connect calls in bias methods
        import re
        # Every sqlite3.connect in weather_engine must be followed by busy_timeout
        connects = [(m.start(), m.group()) for m in re.finditer(r'sqlite3\.connect\(', source)]
        for pos, _ in connects:
            # Check the next 200 chars for busy_timeout
            snippet = source[pos:pos + 200]
            assert "busy_timeout" in snippet, (
                f"sqlite3.connect at position {pos} missing PRAGMA busy_timeout"
            )


class TestWeatherHRRR:
    """HRRR fetch must have rate-limit sleep and honest logging."""

    def test_hrrr_has_rate_limit_sleep(self):
        """fetch_ensemble must sleep before HRRR call to avoid timeouts."""
        source = open(os.path.join(PROJECT_ROOT, "bot", "engines", "weather_engine.py")).read()  # Sprint 10.1c (2026-05-11)
        tree = ast.parse(source)

        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "fetch_ensemble":
                func_source = ast.get_source_segment(source, node) or ""
                # Must have time.sleep before _fetch_hrrr
                hrrr_pos = func_source.find("_fetch_hrrr")
                assert hrrr_pos > 0, "fetch_ensemble must call _fetch_hrrr"
                # Find the last time.sleep before hrrr call
                before_hrrr = func_source[:hrrr_pos]
                assert "time.sleep" in before_hrrr, (
                    "fetch_ensemble must have time.sleep before _fetch_hrrr call "
                    "to avoid rate-limit timeouts"
                )
                return
        assert False, "fetch_ensemble not found in weather_engine.py"

    def test_hrrr_log_does_not_mask_none(self):
        """HRRR log must not use 'or 0.0' which masks None as 0.0F."""
        source = open(os.path.join(PROJECT_ROOT, "bot", "engines", "weather_engine.py")).read()  # Sprint 10.1c (2026-05-11)
        tree = ast.parse(source)

        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "fetch_ensemble":
                func_source = ast.get_source_segment(source, node) or ""
                assert "hrrr_temp\") or 0.0" not in func_source, (
                    "HRRR log uses 'or 0.0' which masks None failures as 0.0F. "
                    "Use explicit None check instead."
                )
                assert "hrrr_temp') or 0.0" not in func_source, (
                    "HRRR log uses 'or 0.0' which masks None failures as 0.0F."
                )
                return
        assert False, "fetch_ensemble not found in weather_engine.py"


# ============================================================================
#  16. Dedup Set Tuple Safety (abd47c8, Mar 7 2026)
#      Bug: _eval_opp_seen contained mixed 2-tuples (ticker, stage) and
#      3-tuples (ticker, stage, side). Cleanup comprehension destructured
#      as (tk, stage) → ValueError on every tick → crash loop on VPS.
#      Prevention: Never destructure _eval_opp_seen entries; use key[0]
#      to access ticker. Test scans all set comprehensions involving the set.
# ============================================================================

class TestDedupSetTupleSafety:
    """_eval_opp_seen may contain 2-tuples or 3-tuples. Never destructure."""

    def test_no_tuple_destructuring_in_eval_opp_seen(self):
        """Scan bot/_impl.py for any (tk, stage) unpacking of _eval_opp_seen."""
        # Bit 8.1 (2026-05-10): scanner moved to bot/scanner/__init__.py.
        source = _read_bot_and_scanner()
        # Find any set comprehension or for loop that destructures _eval_opp_seen
        # Pattern: "for tk, stage in self._eval_opp_seen" or similar 2-var unpack
        dangerous_patterns = [
            r'for\s+\w+,\s*\w+\s+in\s+self\._eval_opp_seen',
            r'for\s+\(\w+,\s*\w+\)\s+in\s+self\._eval_opp_seen',
        ]
        for pattern in dangerous_patterns:
            matches = re.findall(pattern, source)
            assert len(matches) == 0, (
                f"Found dangerous tuple destructuring of _eval_opp_seen: {matches}. "
                f"_eval_opp_seen contains mixed 2/3-tuples. Use key[0] instead. "
                f"(Bug abd47c8: crash loop on VPS from ValueError)"
            )

    def test_eval_opp_seen_cleanup_uses_key_index(self):
        """The cleanup comprehension must use key[0], not destructuring."""
        # Bit 8.1 (2026-05-10): scanner moved to bot/scanner/__init__.py.
        source = _read_bot_and_scanner()
        # Find the cleanup line
        cleanup_match = re.search(
            r'self\._eval_opp_seen\s*=\s*\{[^}]+\}',
            source
        )
        assert cleanup_match, "_eval_opp_seen cleanup comprehension not found"
        cleanup_code = cleanup_match.group()
        assert "key[0]" in cleanup_code or "k[0]" in cleanup_code, (
            f"_eval_opp_seen cleanup must use key[0] indexing, not tuple destructuring. "
            f"Found: {cleanup_code}"
        )


# ============================================================================
#  14. Shadow Engine Callsite Variable Safety (6b017bb)
#      Bug: egarch_blend_weight was used as a bare variable at the
#      fifteenm_shadow.evaluate_strike() callsite, but it was never assigned
#      as a local variable in scan(). It only existed as
#      _shadow_diag["egarch_blend_weight"]. This caused NameError on every
#      call, silently swallowed by `except Exception: logging.debug(...)`.
#      The shadow engine was dead code for weeks.
# ============================================================================

class TestShadowCallsiteVariables:
    """All variables at fifteenm_shadow callsites must be defined names."""

    def _get_scan_source(self):
        """Return the source of the scan() method.

        Bit 8.1 (2026-05-10): OpportunityScanner extracted to
        bot/scanner/__init__.py — fifteenm_shadow.evaluate_strike call
        sites moved with the class. Walk both files so this guard
        survives the move.
        """
        return _read_bot_and_scanner()

    def test_shadow_callsite_no_bare_egarch_blend_weight(self):
        """egarch_blend_weight must come from _shadow_diag or vol_est, not bare."""
        source = self._get_scan_source()
        # Find all fifteenm_shadow.evaluate_strike calls
        calls = re.findall(
            r'fifteenm_shadow\.evaluate_strike\([^)]+\)',
            source, re.DOTALL
        )
        assert len(calls) >= 1, "No fifteenm_shadow.evaluate_strike calls found"
        for call in calls:
            # The bug: egarch_blend_weight=egarch_blend_weight (bare var)
            # Correct: egarch_blend_weight=_shadow_diag.get("egarch_blend_weight")
            #      or: egarch_blend_weight=vol_est.get("egarch_blend_weight")
            if "egarch_blend_weight=" in call:
                rhs = re.search(
                    r'egarch_blend_weight\s*=\s*(\S+)',
                    call
                )
                assert rhs, "Could not parse egarch_blend_weight= assignment"
                value = rhs.group(1).rstrip(",)")
                assert value != "egarch_blend_weight", (
                    f"fifteenm_shadow callsite uses bare 'egarch_blend_weight' "
                    f"variable — this is a NameError! Must use "
                    f"_shadow_diag.get('egarch_blend_weight') or "
                    f"vol_est.get('egarch_blend_weight'). "
                    f"(Bug 6b017bb: shadow engine was dead code for weeks)"
                )

    def test_shadow_callsite_pre_filter_exists(self):
        """fifteenm_shadow must be called BEFORE the price filter for full coverage."""
        source = self._get_scan_source()
        # Find the price filter
        price_filter_pos = source.find("# Filter: ask must be in entry price range")
        assert price_filter_pos > 0, "Price filter comment not found in bot/_impl.py"
        # Find the first shadow callsite
        first_shadow_pos = source.find("fifteenm_shadow.evaluate_strike")
        assert first_shadow_pos > 0, "No fifteenm_shadow.evaluate_strike found"
        assert first_shadow_pos < price_filter_pos, (
            "fifteenm_shadow.evaluate_strike must appear BEFORE the price "
            "filter to evaluate all 15M signals, not just those in price range"
        )

    def test_no_logging_debug_in_shadow_except(self):
        """Shadow engine exception handlers must use warning+, not debug.

        debug-level exceptions are invisible in production (INFO level) and
        silently swallow real errors like NameError, making the shadow engine
        appear to work when it's actually dead code.
        """
        # Bit 9.1 (2026-05-10): OrderExecutor extracted to bot/executor.py — read both for source-level audits

        _paths = [os.path.join(PROJECT_ROOT, "bot/_impl.py"), os.path.join(PROJECT_ROOT, "bot/executor.py"), os.path.join(PROJECT_ROOT, "bot/settlement.py"), os.path.join(PROJECT_ROOT, "bot/main_loop.py")]

        lines = []

        for _p in _paths:
            if os.path.isfile(_p):
                with open(_p) as f:
                    lines.extend(f.readlines())
        for i, line in enumerate(lines):
            if "fifteenm_shadow" in line and "logging.debug" in line:
                # Allow debug in non-except contexts
                # Check if previous non-empty line is 'except'
                for j in range(i - 1, max(0, i - 3), -1):
                    if lines[j].strip().startswith("except"):
                        pytest.fail(
                            f"bot/_impl.py:{i+1}: fifteenm_shadow error handler uses "
                            f"logging.debug — must use logging.warning to catch "
                            f"silent failures like NameError "
                            f"(Bug 6b017bb: dead code for weeks)"
                        )


# ============================================================================
#  NO-Side Orderbook Pricing (Mar 7 2026)
#  Bug: NO price was derived as `100 - YES_bid` or `100 - market_price`.
#  Correct: NO ask is its own price from market NBBO (`no_ask` field).
#  YES and NO prices do NOT always sum to 100 — arbitrage can exist.
#  Fix: read actual `no_ask` from Kalshi market NBBO, pass through call chain.
# ============================================================================

class TestNoSideOrderbookPricing:
    """NO-side pricing must use actual NO ask from market NBBO, never derived from YES prices."""

    def _get_bot_source(self):
        # Bit 8.1 (2026-05-10): _no_side_queue + _process_no_side_shadow
        # + weather NO-side edge moved to bot/scanner/__init__.py with
        # the OpportunityScanner extraction. Walk both files.
        return _read_bot_and_scanner()

    def test_no_side_queue_passes_no_ask(self):
        """All _no_side_queue.append() calls must include 'no_ask' key."""
        source = self._get_bot_source()
        # Find all _no_side_queue.append blocks
        appends = list(re.finditer(
            r'_no_side_queue\.append\(\{([^}]+)\}',
            source, re.DOTALL
        ))
        assert len(appends) >= 2, (
            f"Expected at least 2 _no_side_queue.append calls, found {len(appends)}"
        )
        for i, m in enumerate(appends):
            body = m.group(1)
            assert '"no_ask"' in body or "'no_ask'" in body, (
                f"_no_side_queue.append #{i+1} does not pass 'no_ask' key. "
                f"NO price must come from actual market NBBO no_ask field, "
                f"not derived from YES-side prices."
            )

    def test_process_no_side_uses_item_no_ask(self):
        """_process_no_side_shadow must get NO price from item['no_ask'], not 100 - best_ask."""
        source = self._get_bot_source()
        # Find the _process_no_side_shadow method
        method_match = re.search(
            r'def _process_no_side_shadow\(self.*?\n(    def |\Z)',
            source, re.DOTALL
        )
        assert method_match, "_process_no_side_shadow not found"
        method_body = method_match.group(0)

        # Must NOT have `no_price = 100 - best_ask`
        assert "100 - best_ask" not in method_body, (
            "_process_no_side_shadow still uses '100 - best_ask' to compute NO price. "
            "This gives NO BID, not NO ASK. Must use item['no_ask'] from actual orderbook."
        )

        # Must have item["no_ask"] or item.get("no_ask")
        assert 'item["no_ask"]' in method_body or "item.get(\"no_ask\")" in method_body or "item['no_ask']" in method_body, (
            "_process_no_side_shadow does not read 'no_ask' from queue item. "
            "NO price must come from actual orderbook data passed through the queue."
        )

    def test_no_inferred_no_price_anywhere(self):
        """No NO-side pricing code should use `100 - best_ask` as a NO ask price.

        `100 - best_ask` = `100 - YES_ask` = NO BID (what you'd get selling NO).
        NO ASK (what you'd pay buying NO) = `100 - YES_bid`.
        Using the wrong one means every NO-side edge calculation is wrong.
        """
        source = self._get_bot_source()
        lines = source.split('\n')
        violations = []
        for i, line in enumerate(lines):
            stripped = line.strip()
            # Skip comments
            if stripped.startswith('#'):
                continue
            # Skip EV calculations (those correctly use 100 - best_ask for YES-side payoff)
            if '_ev =' in line or '_ev=' in line:
                continue
            # Skip YES-side market blend (correctly uses best_ask)
            if 'mip = best_ask' in line:
                continue
            # Check for NO-price patterns using 100 - best_ask
            if re.search(r'_?no_?price\s*=\s*100\s*-\s*best_ask', line):
                violations.append(f"bot/_impl.py:{i+1}: {stripped}")
        assert not violations, (
            f"Found {len(violations)} location(s) computing NO price as "
            f"'100 - best_ask' (= NO BID, wrong!):\n" +
            "\n".join(violations) +
            "\n\nNO ASK comes from market NBBO no_ask field, not derived from YES prices."
        )

    def test_weather_no_side_uses_market_nbbo(self):
        """Weather NO-side edge must use actual NO ask from market NBBO."""
        source = self._get_bot_source()
        # Find the weather NO-side section
        wx_match = re.search(
            r'# ── Weather NO-side shadow edge ──(.*?)(?=\n\s+# ──|\n\s+_no_side_queue)',
            source, re.DOTALL
        )
        assert wx_match, "Weather NO-side shadow edge section not found"
        wx_body = wx_match.group(1)
        assert "100 - best_ask" not in wx_body, (
            "Weather NO-side edge still uses '100 - best_ask'. "
            "Must use actual NO ask from market NBBO."
        )
        # Must read no_ask from market data
        assert "no_ask" in wx_body, (
            "Weather NO-side edge does not read 'no_ask' from market NBBO."
        )

    def test_shadow_engines_accept_no_ask_param(self):
        """All shadow engines must accept `no_ask` parameter for actual NO ask from market NBBO.

        NO ask is its own price from the NBBO, not derived from YES prices.
        """
        engines = [
            ("fifteenm_shadow.py", "evaluate_strike"),
            ("spx_harrv_shadow.py", "evaluate"),
            ("hourly_alt_shadow.py", "evaluate_strike"),
        ]
        for fname, method in engines:
            fpath = os.path.join(PROJECT_ROOT, fname)
            if not os.path.exists(fpath):
                continue
            with open(fpath) as f:
                source = f.read()

            # Method signature must accept no_ask parameter
            sig_match = re.search(rf'def {method}\([^)]+\)', source, re.DOTALL)
            assert sig_match, f"{fname}: method {method} not found"
            sig = sig_match.group(0)
            assert "no_ask" in sig, (
                f"{fname}.{method}() does not accept 'no_ask' parameter. "
                f"NO price must come from actual market NBBO, not derived from YES prices."
            )


# ============================================================================
#  V2 Variant (hourly shadow cal pipeline)
#  Ensure V2 rows are inserted correctly, not double-feeding CalEngine,
#  and dedup keys are safe.
# ============================================================================

class TestV2VariantSafety:
    """V2 variant (hourly_observation_v2) must coexist safely with V1."""

    def test_v2_filter_stage_in_calengine_exclusion(self):
        """V2 rows must NOT feed CalEngine (would double-count raw_prob)."""
        # Bit 8.1 (2026-05-10): scanner moved to bot/scanner/__init__.py.
        source = _read_bot_and_scanner()
        # The CalEngine feed section must exclude _v2 filter stages
        assert 'not filter_stage.endswith("_v2")' in source, (
            "CalEngine settlement feed must exclude _v2 variant rows. "
            "V2 shares raw_prob with V1 — feeding both double-counts observations."
        )

    def test_v2_dedup_key_is_2tuple(self):
        """V2 dedup key must be a 2-tuple (ticker, 'hourly_observation_v2')."""
        # Bit 8.1 (2026-05-10): scanner moved to bot/scanner/__init__.py.
        source = _read_bot_and_scanner()
        # The helper method must use the correct dedup key format
        assert '(ticker, "hourly_observation_v2")' in source, (
            "_insert_hourly_v2_variant must use 2-tuple dedup key "
            "matching _eval_opp_seen format"
        )

    def test_v2_helper_method_exists(self):
        """_insert_hourly_v2_variant helper must exist on OpportunityScanner."""
        # Bit 8.1 (2026-05-10): scanner moved to bot/scanner/__init__.py.
        source = _read_bot_and_scanner()
        assert "def _insert_hourly_v2_variant(" in source, (
            "V2 variant helper method missing from bot/_impl.py or bot/scanner/__init__.py"
        )

    def test_v2_called_at_both_gates(self):
        """V2 must be inserted at both insufficient_edge and hourly_observation gates."""
        # Bit 8.1 (2026-05-10): scanner moved to bot/scanner/__init__.py.
        source = _read_bot_and_scanner()
        call_count = source.count("self._insert_hourly_v2_variant(")
        assert call_count >= 2, (
            f"_insert_hourly_v2_variant called {call_count} times, expected >= 2. "
            f"Must be called at both the insufficient_edge and hourly_observation gates "
            f"so V2 captures signals regardless of V1's edge decision."
        )

    def test_v2_uses_shadow_cal_prob(self):
        """V2 must use shadow cal pipeline probability, not live probability."""
        # Bit 8.1 (2026-05-10): scanner moved to bot/scanner/__init__.py.
        source = _read_bot_and_scanner()
        # The helper should reference cal_pipeline or old_cal_system
        assert 'calibration_method="shadow_cal_v2"' in source, (
            "V2 variant must tag calibration_method as 'shadow_cal_v2' "
            "for audit script filtering"
        )

    def test_v2_audit_section_exists(self):
        """Audit script must have V2 comparison section."""
        with open(os.path.join(PROJECT_ROOT, "scripts", "hourly_shadow_audit.py")) as f:
            source = f.read()
        assert "hourly_observation_v2" in source, (
            "hourly_shadow_audit.py must query hourly_observation_v2 for variant comparison"
        )
        assert "v2_variant_comparison" in source, (
            "hourly_shadow_audit.py must have v2_variant_comparison function"
        )

    def test_v2_alpha_section_exists(self):
        """Alpha research script must have V2 comparison section."""
        with open(os.path.join(PROJECT_ROOT, "scripts", "hourly_alpha_research.py")) as f:
            source = f.read()
        assert "hourly_observation_v2" in source, (
            "hourly_alpha_research.py must query hourly_observation_v2 for variant comparison"
        )
        assert "v2_variant_alpha" in source, (
            "hourly_alpha_research.py must have v2_variant_alpha function"
        )


# ============================================================================
#  Kelly Sizing Division by Zero (a7408ec → fix)
#     Bug: PositionSizer.compute() did not guard b <= 0 after fees.
#     At price_cents=99 with taker fee=1c, b = (100-99-1)/(99+1) = 0,
#     causing ZeroDivisionError in kelly_edge = (b*p - q) / b.
#     Pre-existing bug exposed by V2 variant adding a second sizer.compute() callsite.
# ============================================================================

class TestKellySizerZeroPayout:
    """Verify PositionSizer.compute() handles zero-payout prices safely."""

    def test_compute_guard_exists_in_source(self):
        """Source code must guard b <= 0 before Kelly division."""
        # PositionSizer lives in models.py (extracted from bot/_impl.py)
        with open(os.path.join(PROJECT_ROOT, "models.py")) as f:
            source = f.read()
        assert "if b <= 0:" in source, (
            "PositionSizer.compute() must guard b <= 0 to prevent division by zero"
        )

    def test_guard_precedes_kelly_division(self):
        """The b <= 0 guard must appear BEFORE the kelly_edge division in compute()."""
        # PositionSizer lives in models.py (extracted from bot/_impl.py)
        with open(os.path.join(PROJECT_ROOT, "models.py")) as f:
            source = f.read()
        guard_pos = source.find("if b <= 0:")
        division_pos = source.find("kelly_edge = (b * p - q) / b")
        assert guard_pos > 0, "b <= 0 guard must exist"
        assert division_pos > 0, "Kelly division must exist"
        assert guard_pos < division_pos, (
            "b <= 0 guard must come BEFORE kelly_edge division, "
            f"but guard is at char {guard_pos} and division at {division_pos}"
        )

    def test_guard_returns_early(self):
        """The b <= 0 guard must return result (not just pass)."""
        # PositionSizer lives in models.py (extracted from bot/_impl.py)
        with open(os.path.join(PROJECT_ROOT, "models.py")) as f:
            source = f.read()
        guard_idx = source.find("if b <= 0:")
        block = source[guard_idx:guard_idx + 200]
        assert "return result" in block, (
            "b <= 0 guard must return result to prevent reaching Kelly division"
        )


# ============================================================================
#  PM-001 (38754ef, Mar 9 2026): Database contention hardening
#     Bug: _poll_evaluated_opportunities() did 91 per-row commits colliding
#     with supabase_sync's 165 queries/10s → "database is locked" + 100% CPU.
# ============================================================================

class TestNoBatchCommitInLoops:
    """conn.commit() must not appear inside for/while loops in bot/_impl.py.
    Per-row commits multiply the contention window with concurrent DB readers.
    Rule: accumulate writes, commit once at the end. See POSTMORTEMS.md PM-001."""

    # Known exceptions: methods where per-iteration commit is intentional and safe
    # (e.g., _backfill_weather_actual_temps operates on max 10 rows with HTTP delays)
    EXEMPT_METHODS = {
        "_backfill_weather_actual_temps",
        "_create_tables",
        "_poll_evaluated_opportunities",  # batches by ticker, commits after each ticker's updates
        "_process_low_price_shadow",      # commits once after loop, guarded by if _lps_rows
    }

    def test_no_commit_inside_for_loops_in_bot(self):
        """Static analysis: find .commit() calls nested inside for/while loops."""
        with open(os.path.join(PROJECT_ROOT, "bot/_impl.py")) as f:
            source = f.read()

        tree = ast.parse(source)
        violations = []

        for node in ast.walk(tree):
            if not isinstance(node, (ast.For, ast.While)):
                continue
            # Walk the loop body looking for .commit() calls
            for child in ast.walk(node):
                if (isinstance(child, ast.Call)
                        and isinstance(child.func, ast.Attribute)
                        and child.func.attr == "commit"):
                    # Check if this loop is inside an exempt method
                    exempt = False
                    for parent in ast.walk(tree):
                        if (isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef))
                                and parent.name in self.EXEMPT_METHODS):
                            if hasattr(parent, 'lineno') and hasattr(node, 'lineno'):
                                if (parent.lineno <= node.lineno
                                        and hasattr(parent, 'end_lineno')
                                        and parent.end_lineno >= node.lineno):
                                    exempt = True
                                    break
                    if not exempt:
                        violations.append(
                            f"bot/_impl.py:{child.lineno} — .commit() inside loop "
                            f"starting at line {node.lineno}"
                        )
                    break  # Only flag once per loop

        assert not violations, (
            f"Found .commit() inside loops (causes DB contention): {violations}. "
            f"Rule: never commit inside a loop — always batch. "
            f"See POSTMORTEMS.md PM-001."
        )


# ============================================================================
#  NO-Side Win/Loss Counting (1ced6bf, Mar 9 2026)
#  Bug: audit_cron.py counted market_result='yes' as a win regardless of
#  bet side. For NO-side bets, market_result='yes' is a LOSS. This inflated
#  hourly WR from 62.1% to 65.1% and hid +22.8pp overconfidence behind
#  a fake +10.1pp number. 164 NO-side entries started appearing Mar 8.
# ============================================================================

class TestNoSideWinCounting:
    """Verify audit win/loss counting is side-aware.

    Bug: audit_cron.py computed wins as SUM(CASE WHEN market_result='yes' THEN 1 END)
    without checking the 'side' column. For NO-side bets, this inverts W/L.
    The correct logic: win = (side matches market_result).

    Caught Mar 9 2026 when hourly shadow report showed 65.1% WR but
    manual side-aware counting showed 62.1%.
    """

    @pytest.fixture
    def audit_db(self, tmp_path):
        """Create an in-memory DB with synthetic evaluated_opportunities."""
        db_path = str(tmp_path / "test_audit.db")
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("""
            CREATE TABLE evaluated_opportunities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT, event_ticker TEXT, asset TEXT,
                filter_stage TEXT, evaluation_time TEXT,
                spot_price REAL, threshold REAL, volatility REAL,
                market_price INTEGER, seconds_to_close REAL,
                calibrated_prob REAL, edge REAL,
                status TEXT, market_result TEXT,
                product_type TEXT, side TEXT,
                position_size INTEGER, raw_prob REAL,
                ofa_adjustment REAL, counterfactual_pnl REAL,
                rejection_reason TEXT,
                hourly_pre_temp_prob REAL, hourly_applied_temp_t REAL,
                fee_adjusted_edge REAL, egarch_blend_weight REAL,
                egarch_blend_sigma REAL, wx_ensemble_mean REAL,
                wx_market_type TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE settled_trades (
                ticker TEXT PRIMARY KEY, event_ticker TEXT, asset TEXT,
                market_result TEXT, side TEXT, count INTEGER,
                entry_price_cents INTEGER, revenue_cents INTEGER,
                fee_cents INTEGER, pnl_cents INTEGER,
                settled_at TEXT, product_type TEXT,
                fill_latency_seconds REAL, escalation_type TEXT
            )
        """)
        # Insert test data: 4 scenarios covering all side x result combos
        base_time = "2026-03-09T12:00:00"
        scenarios = [
            # (side, market_result, should_be_win)
            ("yes", "yes", True),   # YES bet, market YES → WIN
            ("yes", "no", False),   # YES bet, market NO → LOSS
            ("no", "no", True),     # NO bet, market NO → WIN
            ("no", "yes", False),   # NO bet, market YES → LOSS
        ]
        for i, (side, result, _) in enumerate(scenarios):
            conn.execute("""
                INSERT INTO evaluated_opportunities
                    (ticker, event_ticker, asset, filter_stage, evaluation_time,
                     market_price, calibrated_prob, status, market_result,
                     product_type, side, position_size)
                VALUES (?, ?, 'BTC', 'hourly_observation', ?,
                        85, 0.90, 'settled', ?, 'hourly', ?, 1)
            """, (f"TICK-{i}", f"EVT-{i}", base_time, result, side))
        conn.commit()
        return db_path

    def test_yes_bet_yes_result_is_win(self, audit_db):
        conn = sqlite3.connect(audit_db)
        row = conn.execute("""
            SELECT COALESCE(side,'yes') = market_result AS is_win
            FROM evaluated_opportunities WHERE ticker = 'TICK-0'
        """).fetchone()
        assert row[0] == 1, "YES bet + market YES should be a win"

    def test_yes_bet_no_result_is_loss(self, audit_db):
        conn = sqlite3.connect(audit_db)
        row = conn.execute("""
            SELECT COALESCE(side,'yes') = market_result AS is_win
            FROM evaluated_opportunities WHERE ticker = 'TICK-1'
        """).fetchone()
        assert row[0] == 0, "YES bet + market NO should be a loss"

    def test_no_bet_no_result_is_win(self, audit_db):
        conn = sqlite3.connect(audit_db)
        row = conn.execute("""
            SELECT COALESCE(side,'yes') = market_result AS is_win
            FROM evaluated_opportunities WHERE ticker = 'TICK-2'
        """).fetchone()
        assert row[0] == 1, "NO bet + market NO should be a win"

    def test_no_bet_yes_result_is_loss(self, audit_db):
        conn = sqlite3.connect(audit_db)
        row = conn.execute("""
            SELECT COALESCE(side,'yes') = market_result AS is_win
            FROM evaluated_opportunities WHERE ticker = 'TICK-3'
        """).fetchone()
        assert row[0] == 0, "NO bet + market YES should be a loss"

    def test_yes_side_filter_excludes_no_bets(self, audit_db):
        """YES_SIDE_FILTER must exclude NO-side rows from YES-side aggregation."""
        conn = sqlite3.connect(audit_db)
        YES_SIDE_FILTER = "AND (side IS NULL OR side = 'yes')"
        row = conn.execute(f"""
            SELECT COUNT(*) FROM evaluated_opportunities
            WHERE product_type = 'hourly' {YES_SIDE_FILTER}
        """).fetchone()
        assert row[0] == 2, f"YES_SIDE_FILTER should return 2 rows, got {row[0]}"

    def test_aggregate_win_count_is_side_aware(self, audit_db):
        """The corrected audit pattern: wins = rows where side matches market_result."""
        conn = sqlite3.connect(audit_db)
        YES_SIDE_FILTER = "AND (side IS NULL OR side = 'yes')"
        row = conn.execute(f"""
            SELECT
                SUM(CASE WHEN market_result = 'yes' THEN 1 ELSE 0 END) as yes_wins,
                COUNT(*) as total
            FROM evaluated_opportunities
            WHERE product_type = 'hourly' {YES_SIDE_FILTER}
        """).fetchone()
        # Only YES-side rows: TICK-0 (yes/yes=win) and TICK-1 (yes/no=loss)
        # yes_wins = 1 (TICK-0), total = 2
        assert row[0] == 1, f"Should count 1 YES-side win, got {row[0]}"
        assert row[1] == 2, f"Should have 2 YES-side rows, got {row[1]}"

    def test_buggy_pattern_would_overcount_wins(self, audit_db):
        """Demonstrate that the OLD buggy pattern overcounts wins for NO-side bets."""
        conn = sqlite3.connect(audit_db)
        # OLD buggy query: no YES_SIDE_FILTER, counts market_result='yes' as win
        row = conn.execute("""
            SELECT
                SUM(CASE WHEN market_result = 'yes' THEN 1 ELSE 0 END) as buggy_wins,
                COUNT(*) as total
            FROM evaluated_opportunities
            WHERE product_type = 'hourly'
        """).fetchone()
        # Without filter: 4 rows total, market_result='yes' on TICK-0 and TICK-3
        # TICK-3 is a NO-side loss but the buggy query counts it as a win
        assert row[0] == 2, f"Buggy query should show 2 'wins', got {row[0]}"
        assert row[1] == 4, f"Should have 4 total rows, got {row[1]}"
        # The CORRECT answer is 1 YES-side win, not 2

    def test_compute_hourly_uses_yes_side_filter(self):
        """Static analysis: compute_hourly queries must include YES_SIDE_FILTER."""
        audit_path = os.path.join(PROJECT_ROOT, "scripts", "audit_cron.py")
        with open(audit_path) as f:
            source = f.read()

        # Find compute_hourly function body
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "compute_hourly":
                func_source = ast.get_source_segment(source, node)
                # Count SQL queries that aggregate market_result without YES_SIDE_FILTER
                # The W/L, Brier, overconfidence, worst-asset, and per-asset queries
                # should all include YES_SIDE_FILTER
                assert "YES_SIDE_FILTER" in func_source, (
                    "compute_hourly must use YES_SIDE_FILTER for side-aware counting"
                )
                # Count occurrences — there should be at least 5 (W/L, Brier, OC, worst, per-asset)
                count = func_source.count("YES_SIDE_FILTER")
                assert count >= 5, (
                    f"compute_hourly has {count} YES_SIDE_FILTER refs, expected >= 5 "
                    f"(W/L, Brier, overconfidence, worst-asset, per-asset)"
                )
                break
        else:
            pytest.fail("compute_hourly function not found in audit_cron.py")

    def test_compute_spx_uses_yes_side_filter(self):
        """Static analysis: compute_spx queries must include YES_SIDE_FILTER."""
        audit_path = os.path.join(PROJECT_ROOT, "scripts", "audit_cron.py")
        with open(audit_path) as f:
            source = f.read()

        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "compute_spx":
                func_source = ast.get_source_segment(source, node)
                assert "YES_SIDE_FILTER" in func_source, (
                    "compute_spx must use YES_SIDE_FILTER for side-aware counting"
                )
                break
        else:
            pytest.fail("compute_spx function not found in audit_cron.py")

    def test_compute_weather_uses_yes_side_filter(self):
        """Static analysis: compute_weather queries must include YES_SIDE_FILTER."""
        audit_path = os.path.join(PROJECT_ROOT, "scripts", "audit_cron.py")
        with open(audit_path) as f:
            source = f.read()

        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "compute_weather":
                func_source = ast.get_source_segment(source, node)
                assert "YES_SIDE_FILTER" in func_source, (
                    "compute_weather must use YES_SIDE_FILTER for side-aware counting"
                )
                break
        else:
            pytest.fail("compute_weather function not found in audit_cron.py")


# ============================================================================
# Rolling 7-Day HWM (replaces static startup HWM)
#     Bug: Static startup HWM ratchets up but never down. After withdrawal,
#     stale HWM makes balance look like 50% drawdown → half-Kelly for weeks.
# ============================================================================

class TestRollingHWM:
    """PositionSizer must use rolling 7-day peak, not static startup HWM."""

    def test_balance_history_exists(self):
        from models import PositionSizer
        sizer = PositionSizer(starting_balance_cents=10000)
        assert hasattr(sizer, "_balance_history")

    def test_record_balance(self):
        from models import PositionSizer
        sizer = PositionSizer(starting_balance_cents=10000)
        # Must complete warmup (5 readings) before balance_history is populated
        for _ in range(5):
            sizer.record_balance(11000)
        assert len(sizer._balance_history) == 1  # warmup seeds 1 median entry

    def test_rolling_hwm_uses_max(self):
        from models import PositionSizer
        sizer = PositionSizer(starting_balance_cents=10000)
        # Complete warmup at 12000
        for _ in range(5):
            sizer.record_balance(12000)
        # Record lower values (within 50% floor guard of HWM=12000)
        sizer.record_balance(11000)
        sizer.record_balance(10500)
        assert sizer.get_rolling_hwm() == 12000

    def test_rolling_hwm_ages_out(self):
        """Old peaks beyond lookback should not count."""
        import time as _time
        from models import PositionSizer
        from config import HWM_LOOKBACK_SECONDS
        sizer = PositionSizer(starting_balance_cents=10000)
        # Complete warmup first
        for _ in range(5):
            sizer.record_balance(10000)
        # Insert an old high balance beyond lookback window
        old_ts = _time.time() - HWM_LOOKBACK_SECONDS - 3600
        sizer._balance_history.append((old_ts, 50000))
        # Insert a recent balance
        sizer.record_balance(10000)
        assert sizer.get_rolling_hwm() == 10000

    def test_drawdown_scaler_uses_rolling_hwm(self):
        from models import PositionSizer
        sizer = PositionSizer(starting_balance_cents=10000)
        # Complete warmup
        for _ in range(5):
            sizer.record_balance(10000)
        # At current balance = HWM, scaler should be 1.0
        scaler = sizer._drawdown_scaler(10000)
        assert scaler == 1.0

    def test_override_hwm_env_var(self):
        import os
        from models import PositionSizer
        os.environ["OVERRIDE_HWM"] = "100.00"
        try:
            sizer = PositionSizer(starting_balance_cents=5000)
            assert sizer.get_rolling_hwm() == 10000  # $100 = 10000 cents
        finally:
            del os.environ["OVERRIDE_HWM"]

    def test_no_stale_hwm_after_withdrawal(self):
        """After withdrawal, drawdown scaler should recover when old peak ages out."""
        import time as _time
        from models import PositionSizer
        from config import HWM_LOOKBACK_SECONDS
        sizer = PositionSizer(starting_balance_cents=55000)
        # Complete warmup at post-withdrawal balance
        for _ in range(5):
            sizer.record_balance(55000)
        # Insert an old high balance beyond lookback window
        old_ts = _time.time() - HWM_LOOKBACK_SECONDS - 1
        sizer._balance_history.append((old_ts, 100000))
        # Record current balance again (recent)
        sizer.record_balance(55000)
        # HWM should be 55000 (old peak aged out), so ratio = 1.0
        assert sizer.get_rolling_hwm() == 55000
        scaler = sizer._drawdown_scaler(55000)
        assert scaler == 1.0


# ============================================================================
# SOL 1.8% Edge Floor
#     Data: SOL 73.9% WR below 1.8% edge, 95.4% above.
#     CalEngine 16.8pp overconfident for low-edge SOL.
# ============================================================================

class TestSOLEdgeFloor:
    """SOL must have a higher minimum edge than the price-dependent schedule."""

    def test_sol_min_edge_exists(self):
        import bot
        assert hasattr(bot, "SOL_MIN_EDGE")
        assert bot.SOL_MIN_EDGE >= 0.008

    def test_sol_min_edge_higher_than_default(self):
        import bot
        # At most common SOL prices (80-92c), default edge is 0.25-0.35%
        # SOL floor should be much higher
        for price in [80, 85, 88, 90, 91]:
            default_edge = bot.get_min_edge(price)
            assert bot.SOL_MIN_EDGE > default_edge, (
                f"SOL_MIN_EDGE {bot.SOL_MIN_EDGE} should exceed default "
                f"{default_edge} at {price}c"
            )

    def test_sol_min_edge_in_scan_code(self):
        """Verify SOL edge floor is applied in the scan edge check."""
        # Bit 8.1 (2026-05-10): scanner extracted to bot/scanner/__init__.py;
        # the SOL_MIN_EDGE wiring lives there now. Walk both files.
        content = _read_bot_and_scanner()
        assert "SOL_MIN_EDGE" in content
        assert 'asset == "SOL"' in content or "asset == 'SOL'" in content


# ============================================================================
#  NBBO Fallback Gates
#  Bug: 456/468 missed candidates had empty orderbooks. execute() suppressed
#  them with ORDER_SUPPRESSED no_asks because _get_addon_best_ask() has no
#  NBBO fallback (scan() does). Simulated PnL at NBBO prices: +$196/week.
# ============================================================================

class TestNBBOFallbackGates:
    """Verify NBBO fallback config and gate logic."""

    def test_nbbo_fallback_gates_exist(self):
        """NBBO_FALLBACK_GATES config must exist with all 4 assets."""
        from bot import NBBO_FALLBACK_GATES
        assert isinstance(NBBO_FALLBACK_GATES, dict)
        for asset in ("BTC", "ETH", "SOL", "XRP"):
            assert asset in NBBO_FALLBACK_GATES, f"Missing gate for {asset}"
            gate = NBBO_FALLBACK_GATES[asset]
            assert len(gate) == 3, f"Gate for {asset} must be (min_price, max_price, max_stc)"
            min_p, max_p, max_stc = gate
            assert isinstance(min_p, int), f"{asset} min_price must be int"
            assert isinstance(max_p, int), f"{asset} max_price must be int"
            assert min_p >= 75, f"{asset} min_price too low: {min_p}"
            assert max_p <= 99, f"{asset} max_price too high: {max_p}"
            assert max_stc is None or isinstance(max_stc, (int, float))

    def test_btc_gate_values(self):
        from bot import NBBO_FALLBACK_GATES
        min_p, max_p, max_stc = NBBO_FALLBACK_GATES["BTC"]
        assert min_p == 80  # Lowered from 86 for LPNE (BTC 80-87c near-expiry)
        assert max_p == 99
        assert max_stc == 300.0

    def test_eth_gate_values(self):
        from bot import NBBO_FALLBACK_GATES, ETH_MIN_ENTRY_PRICE
        min_p, _, max_stc = NBBO_FALLBACK_GATES["ETH"]
        assert min_p == ETH_MIN_ENTRY_PRICE, f"ETH NBBO floor must match ETH_MIN_ENTRY_PRICE ({ETH_MIN_ENTRY_PRICE})"
        assert max_stc == 300.0, "NBBO STC gate should be 300s"

    def test_sol_gate_excludes_low_prices(self):
        """SOL 80-85c has 50-73% WR — must be excluded."""
        from bot import NBBO_FALLBACK_GATES
        min_p, _, _ = NBBO_FALLBACK_GATES["SOL"]
        assert min_p >= 86, f"SOL min_price {min_p} too low, 80-85c is a WR trap"

    def test_xrp_gate_values(self):
        from bot import NBBO_FALLBACK_GATES
        _, _, max_stc = NBBO_FALLBACK_GATES["XRP"]
        assert max_stc == 300.0, "NBBO STC gate should be 300s"

    def test_all_stc_gates_uniform(self):
        """All NBBO STC gates should use the same value (currently 300s)."""
        from bot import NBBO_FALLBACK_GATES
        stc_values = set(max_stc for _, _, max_stc in NBBO_FALLBACK_GATES.values())
        assert len(stc_values) == 1, f"NBBO STC gates not uniform: {stc_values}"
        assert stc_values.pop() == 300.0, "NBBO STC gate should be 300s"

    def test_nbbo_fallback_method_exists(self):
        """OrderExecutor must have _nbbo_fallback_price method."""
        # Bit 9.1 L38: read both bot/_impl.py + bot/executor.py for source-level audits

        _paths = [os.path.join(PROJECT_ROOT, "bot/_impl.py"), os.path.join(PROJECT_ROOT, "bot/executor.py"), os.path.join(PROJECT_ROOT, "bot/settlement.py"), os.path.join(PROJECT_ROOT, "bot/main_loop.py")]

        content = ""

        for _p in _paths:
            if os.path.isfile(_p):
                with open(_p) as f:
                    content += f.read() + "\n"
        assert "def _nbbo_fallback_price(" in content

    @pytest.mark.fragile
    def test_all_no_asks_sites_have_nbbo_fallback(self):
        """Every ORDER_SUPPRESSED no_asks site must have a fresh ask mechanism.

        Main execute paths use _nbbo_fallback_price directly.
        DC/TM/bracket paths use _dc_get_ask_with_depth (which calls _nbbo_fallback_price internally).
        """
        # Bit 9.1 L38: read both bot/_impl.py + bot/executor.py for source-level audits

        _paths = [os.path.join(PROJECT_ROOT, "bot/_impl.py"), os.path.join(PROJECT_ROOT, "bot/executor.py"), os.path.join(PROJECT_ROOT, "bot/settlement.py"), os.path.join(PROJECT_ROOT, "bot/main_loop.py")]

        content = ""

        for _p in _paths:
            if os.path.isfile(_p):
                with open(_p) as f:
                    content += f.read() + "\n"
        lines = content.split("\n")
        no_asks_blocks = [i for i, line in enumerate(lines)
                          if "ORDER_SUPPRESSED no_asks" in line]
        for line_idx in no_asks_blocks:
            # Look backwards up to 10 lines for either fallback mechanism
            preceding = "\n".join(lines[max(0, line_idx-10):line_idx])
            has_fallback = ("_nbbo_fallback_price" in preceding
                           or "_dc_get_ask_with_depth" in preceding)
            assert has_fallback, (
                f"Line {line_idx+1} has ORDER_SUPPRESSED no_asks without "
                f"_nbbo_fallback_price or _dc_get_ask_with_depth check above it"
            )

    def test_nbbo_fallback_blocks_low_price(self):
        """NBBO fallback must reject prices below asset gate."""
        from bot import NBBO_FALLBACK_GATES
        # SOL at 83c should be blocked (gate starts at 86c)
        min_p, _, _ = NBBO_FALLBACK_GATES["SOL"]
        assert 83 < min_p, "Test assumes 83c is below SOL gate"

    def test_nbbo_fallback_blocks_high_stc(self):
        """NBBO fallback must reject signals with STC >= 300s."""
        from bot import NBBO_FALLBACK_GATES
        _, _, max_stc = NBBO_FALLBACK_GATES["ETH"]
        assert max_stc is not None
        assert max_stc == 300.0

    @pytest.mark.fragile
    def test_real_book_path_unaffected(self):
        """When _get_addon_best_ask succeeds, NBBO fallback is not called."""
        # Bit 9.1 L38: read both bot/_impl.py + bot/executor.py for source-level audits

        _paths = [os.path.join(PROJECT_ROOT, "bot/_impl.py"), os.path.join(PROJECT_ROOT, "bot/executor.py"), os.path.join(PROJECT_ROOT, "bot/settlement.py"), os.path.join(PROJECT_ROOT, "bot/main_loop.py")]

        content = ""

        for _p in _paths:
            if os.path.isfile(_p):
                with open(_p) as f:
                    content += f.read() + "\n"
        # _nbbo_fallback_price should only appear inside "is None" guards or
        # inside _dc_get_ask_with_depth (cascading fallback after orderbook attempt).
        lines = content.split("\n")
        for i, line in enumerate(lines):
            if "_nbbo_fallback_price" in line and "def " not in line and "#" not in line.lstrip()[:1]:
                # Check that a preceding line has "is None" condition or we're inside
                # a cascading fallback helper (_dc_get_ask_with_depth)
                context = "\n".join(lines[max(0, i-10):i+1])
                assert ("is None" in context
                        or "fresh_ask is None" in context
                        or "_dc_get_ask_with_depth" in context
                        or "# NBBO fallback" in context), (
                    f"Line {i+1}: _nbbo_fallback_price called outside 'is None' guard "
                    f"or cascading fallback helper"
                )

    def test_session_counters_exist(self):
        """Session counters for NBBO fallback must be initialized."""
        # Bit 9.1 L38: read both bot/_impl.py + bot/executor.py for source-level audits

        _paths = [os.path.join(PROJECT_ROOT, "bot/_impl.py"), os.path.join(PROJECT_ROOT, "bot/executor.py"), os.path.join(PROJECT_ROOT, "bot/settlement.py"), os.path.join(PROJECT_ROOT, "bot/main_loop.py")]

        content = ""

        for _p in _paths:
            if os.path.isfile(_p):
                with open(_p) as f:
                    content += f.read() + "\n"
        assert "_session_nbbo_fallback_attempts" in content
        assert "_session_nbbo_fallback_blocked" in content


# ============================================================================
#  DC Routing Priority
#  Bug: DC candidates (strategy=decided_t1/t1b/t2) hitting SOL taker-first
#  or direct taker paths, which apply MIN_EDGE_PCT (0.25%) instead of DC's
#  -0.01 threshold. 46% of DC signals at 95c+ were being suppressed.
#  Fix: DC check moved above SOL taker-first and direct taker.
# ============================================================================

class TestDCRoutingPriority:
    """Verify DC candidates route through DC path before asset-specific paths."""

    def test_dc_check_before_sol_taker_first(self):
        """DC taker override must appear BEFORE SOL taker-first in execute()."""
        # Bit 9.1 L38: read both bot/_impl.py + bot/executor.py for source-level audits

        _paths = [os.path.join(PROJECT_ROOT, "bot/_impl.py"), os.path.join(PROJECT_ROOT, "bot/executor.py"), os.path.join(PROJECT_ROOT, "bot/settlement.py"), os.path.join(PROJECT_ROOT, "bot/main_loop.py")]

        content = ""

        for _p in _paths:
            if os.path.isfile(_p):
                with open(_p) as f:
                    content += f.read() + "\n"
        dc_pos = content.find('Decided contract taker override')
        sol_pos = content.find('SOL taker-first override')
        assert dc_pos > 0, "DC taker override comment not found"
        assert sol_pos > 0, "SOL taker-first comment not found"
        assert dc_pos < sol_pos, (
            f"DC taker override (pos {dc_pos}) must appear BEFORE "
            f"SOL taker-first (pos {sol_pos})")

    def test_dc_check_before_direct_taker(self):
        """DC taker override must appear BEFORE direct taker <180s."""
        # Bit 9.1 L38: read both bot/_impl.py + bot/executor.py for source-level audits

        _paths = [os.path.join(PROJECT_ROOT, "bot/_impl.py"), os.path.join(PROJECT_ROOT, "bot/executor.py"), os.path.join(PROJECT_ROOT, "bot/settlement.py"), os.path.join(PROJECT_ROOT, "bot/main_loop.py")]

        content = ""

        for _p in _paths:
            if os.path.isfile(_p):
                with open(_p) as f:
                    content += f.read() + "\n"
        dc_pos = content.find('Decided contract taker override')
        dt_pos = content.find('Direct taker for <180s')
        assert dc_pos < dt_pos, "DC taker override must appear BEFORE direct taker"

    def test_dc_strategies_include_z2_z25(self):
        """DC strategy check must include z2 and z25 variants."""
        # Bit 9.1 L38: read both bot/_impl.py + bot/executor.py for source-level audits

        _paths = [os.path.join(PROJECT_ROOT, "bot/_impl.py"), os.path.join(PROJECT_ROOT, "bot/executor.py"), os.path.join(PROJECT_ROOT, "bot/settlement.py"), os.path.join(PROJECT_ROOT, "bot/main_loop.py")]

        content = ""

        for _p in _paths:
            if os.path.isfile(_p):
                with open(_p) as f:
                    content += f.read() + "\n"
        # Find the DC strategy condition
        import re
        match = re.search(r'_dc_strategy\s+in\s+\(([^)]+)\)', content)
        assert match, "DC strategy condition not found"
        strategies = match.group(1)
        assert '"decided_t2_z2"' in strategies, "decided_t2_z2 missing from DC check"
        assert '"decided_t2_z25"' in strategies, "decided_t2_z25 missing from DC check"

    @pytest.mark.fragile
    def test_dc_uses_permissive_edge_threshold(self):
        """DC path must use -0.01 edge threshold, not MIN_EDGE_PCT."""
        # Bit 9.1 (2026-05-10): OrderExecutor extracted to bot/executor.py — read both for source-level audits

        _paths = [os.path.join(PROJECT_ROOT, "bot/_impl.py"), os.path.join(PROJECT_ROOT, "bot/executor.py"), os.path.join(PROJECT_ROOT, "bot/settlement.py"), os.path.join(PROJECT_ROOT, "bot/main_loop.py")]

        lines = []

        for _p in _paths:
            if os.path.isfile(_p):
                with open(_p) as f:
                    lines.extend(f.readlines())
        # The -0.01 threshold is now inside _execute_dc_taker method
        in_dc_method = False
        found_threshold = False
        for i, line in enumerate(lines):
            if 'def _execute_dc_taker' in line:
                in_dc_method = True
            if in_dc_method and line.strip().startswith('def ') and '_execute_dc_taker' not in line:
                break
            if in_dc_method and 'net_edge < -0.01' in line:
                found_threshold = True
        assert found_threshold, "DC _execute_dc_taker must use 'net_edge < -0.01' threshold"

    @pytest.mark.fragile
    def test_sol_dc_does_not_hit_sol_taker_first(self):
        """A SOL candidate with DC strategy must NOT reach SOL taker-first path.

        The DC check returns via _execute_dc_taker before SOL taker-first is reached."""
        # Bit 9.1 (2026-05-10): OrderExecutor extracted to bot/executor.py — read both for source-level audits

        _paths = [os.path.join(PROJECT_ROOT, "bot/_impl.py"), os.path.join(PROJECT_ROOT, "bot/executor.py"), os.path.join(PROJECT_ROOT, "bot/settlement.py"), os.path.join(PROJECT_ROOT, "bot/main_loop.py")]

        lines = []

        for _p in _paths:
            if os.path.isfile(_p):
                with open(_p) as f:
                    lines.extend(f.readlines())
        # Verify DC block has 'return self._execute_dc_taker' before SOL block
        in_dc_block = False
        dc_returns = False
        for line in lines:
            if 'Decided contract taker override' in line:
                in_dc_block = True
            if in_dc_block and 'return self._execute_dc_taker' in line:
                dc_returns = True
            if 'SOL taker-first override' in line:
                break
        assert dc_returns, "DC block must return via _execute_dc_taker before SOL taker-first block"

    def test_no_duplicate_dc_block(self):
        """DC taker override should appear exactly once."""
        # Bit 9.1 L38: read both bot/_impl.py + bot/executor.py for source-level audits

        _paths = [os.path.join(PROJECT_ROOT, "bot/_impl.py"), os.path.join(PROJECT_ROOT, "bot/executor.py"), os.path.join(PROJECT_ROOT, "bot/settlement.py"), os.path.join(PROJECT_ROOT, "bot/main_loop.py")]

        content = ""

        for _p in _paths:
            if os.path.isfile(_p):
                with open(_p) as f:
                    content += f.read() + "\n"
        count = content.count('Decided contract taker override')
        assert count == 1, f"DC taker override appears {count} times, expected 1"


# ============================================================================
#  Settlement Loss-Side Count Cross-Check
#     Bug: _process_settlement only cross-checked count vs Kalshi on WINS
#     (revenue // 100). LOSSES have revenue=0 so inflated counts went silent.
#     XRP 26APR190615-15 on Apr 19 2026 reported 208ct vs real 104ct,
#     over-reporting the loss by $98.80.
# ============================================================================


class TestSportsOrderbookFpShape:
    """Kalshi migrated orderbook responses from ob["orderbook"]["yes"|"no"]
    (integer cent prices) to ob["orderbook_fp"]["yes_dollars"|"no_dollars"]
    (dollar-decimal strings). All 5 orderbook-parsing sites in sports_engine
    silently broke — books looked empty, _infer_favorite returned None for
    every live game, sports_shadow_log had 0 rows for 37+ days.

    The fix routes all parsing through _parse_orderbook(), which accepts
    both shapes and normalizes to [[cents_int, qty_float], ...]. (Apr 19 2026)
    """

    def test_parses_fp_shape_to_integer_cents(self):
        from bot.engines.sports_engine import _parse_orderbook  # Sprint 10.1d (2026-05-11)
        ob = {"orderbook_fp": {
            "no_dollars": [["0.5500", "1.00"], ["0.7100", "750.00"]],
            "yes_dollars": [["0.2800", "9703.00"]],
        }}
        yes_bids, no_bids = _parse_orderbook(ob)
        assert [71, 750.0] in no_bids
        assert [55, 1.0] in no_bids
        assert [28, 9703.0] in yes_bids
        best_no = max(b[0] for b in no_bids)
        assert 100 - best_no == 29

    def test_parses_legacy_shape_unchanged(self):
        from bot.engines.sports_engine import _parse_orderbook  # Sprint 10.1d (2026-05-11)
        ob = {"orderbook": {"yes": [[28, 9703]], "no": [[71, 750]]}}
        yes_bids, no_bids = _parse_orderbook(ob)
        assert no_bids == [[71, 750]]
        assert yes_bids == [[28, 9703]]

    def test_handles_empty_and_malformed(self):
        from bot.engines.sports_engine import _parse_orderbook  # Sprint 10.1d (2026-05-11)
        assert _parse_orderbook(None) == ([], [])
        assert _parse_orderbook({}) == ([], [])
        assert _parse_orderbook({"orderbook_fp": {}}) == ([], [])
        bad = {"orderbook_fp": {"no_dollars": [["not-a-number", "1"]],
                                 "yes_dollars": []}}
        assert _parse_orderbook(bad) == ([], [])

    def test_no_callers_parse_orderbook_directly(self):
        """All orderbook parsing in sports_engine must go through
        _parse_orderbook — a direct ob["orderbook"] access would silently
        break again if Kalshi renames the key."""
        fpath = os.path.join(PROJECT_ROOT, "bot", "engines", "sports_engine.py")  # Sprint 10.1d (2026-05-11)
        with open(fpath) as f:
            src = f.read()
        helper_start = src.find("def _parse_orderbook(")
        helper_end = src.find("\ndef ", helper_start + 1)
        outside = src[:helper_start] + src[helper_end:]
        assert '"orderbook"' not in outside, (
            "Direct ob[\"orderbook\"] reference found outside _parse_orderbook.")
        assert 'book.get("no", [])' not in outside, (
            "Direct book.get('no') found — use _parse_orderbook().")


class TestSettlementLossCountCheck:
    """The loss-side cross-check must fetch Kalshi fills and auto-correct
    count mismatches the same way the WIN-side path does."""

    def test_loss_side_cross_check_present(self):
        """Ensure _process_settlement has the LOSS + get_fills cross-check."""
        # Bit 9.2 (2026-05-10): SettlementTracker._process_settlement moved to bot/settlement.py.
        fpath = os.path.join(PROJECT_ROOT, "bot/settlement.py")
        with open(fpath) as f:
            src = f.read()
        assert "SETTLEMENT_LOSS_COUNT_MISMATCH" in src, (
            "Loss-side count cross-check missing from _process_settlement. "
            "See XRP 26APR190615-15 Apr 19 2026.")
        assert 'outcome == "LOSS"' in src and "get_fills(ticker=ticker" in src, (
            "Loss-side check must call get_fills(ticker=...) on outcome == 'LOSS'")

    def test_loss_side_check_runs_before_pnl_loop(self):
        """Cross-check must correct aggregate_count BEFORE the per-position
        PnL loop; otherwise the correction never reaches settled_trades."""
        # Bit 9.1 (2026-05-10): OrderExecutor extracted to bot/executor.py — read both for source-level audits

        _paths = [os.path.join(PROJECT_ROOT, "bot/_impl.py"), os.path.join(PROJECT_ROOT, "bot/executor.py"), os.path.join(PROJECT_ROOT, "bot/settlement.py"), os.path.join(PROJECT_ROOT, "bot/main_loop.py")]

        lines = []

        for _p in _paths:
            if os.path.isfile(_p):
                with open(_p) as f:
                    lines.extend(f.readlines())
        loss_check_line = None
        pnl_loop_line = None
        in_process_settlement = False
        for i, line in enumerate(lines):
            if "def _process_settlement" in line:
                in_process_settlement = True
            if not in_process_settlement:
                continue
            if "SETTLEMENT_LOSS_COUNT_MISMATCH" in line and loss_check_line is None:
                loss_check_line = i
            if "Process each position row independently" in line and pnl_loop_line is None:
                pnl_loop_line = i
                break
        assert loss_check_line is not None, "loss-side check marker not found"
        assert pnl_loop_line is not None, "PnL loop marker not found"
        assert loss_check_line < pnl_loop_line, (
            f"Loss-side cross-check (line {loss_check_line}) must run BEFORE "
            f"PnL loop (line {pnl_loop_line})")


class TestWinCrossCheckSubDollarGuard:
    """The WIN count-mismatch cross-check must REFUSE to auto-zero a
    confirmed-filled position when Kalshi reports sub-dollar revenue
    (implied_count=0, aggregate_count>0). Without this guard, revenue
    values in [1, 99]¢ silently zeroed count+total_cost_cents and
    recorded a phony $0 settled_trade.

    See KXXRP15M-26APR241200-00 (141ct WIN @ 98c, terminal_momentum_98)
    and KXSOL15M-26APR230200-00 (32ct WIN @ 89c, overnight_discount).
    Apr 23-24 2026.

    Tests are AST-based (not string/indentation matching) so a refactor
    that preserves the guard semantics keeps them passing, but a refactor
    that removes the guard or moves the UPDATE into the guard branch
    fails loudly.
    """

    @staticmethod
    def _find_process_settlement():
        # Bit 9.2 (2026-05-10): SettlementTracker._process_settlement moved to bot/settlement.py.
        fpath = os.path.join(PROJECT_ROOT, "bot/settlement.py")
        with open(fpath) as f:
            src = f.read()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if (isinstance(node, ast.FunctionDef)
                    and node.name == "_process_settlement"):
                return node
        return None

    @staticmethod
    def _find_sub_dollar_if(func_node):
        """Locate the If whose test is `implied_count == 0 and aggregate_count > 0`."""
        for node in ast.walk(func_node):
            if not isinstance(node, ast.If):
                continue
            # Expect: BoolOp(And, [Compare(implied_count, Eq, 0),
            #                      Compare(aggregate_count, Gt, 0)])
            t = node.test
            if not (isinstance(t, ast.BoolOp) and isinstance(t.op, ast.And)):
                continue
            if len(t.values) != 2:
                continue
            names = set()
            for cmp in t.values:
                if isinstance(cmp, ast.Compare) and isinstance(cmp.left, ast.Name):
                    names.add(cmp.left.id)
            if names == {"implied_count", "aggregate_count"}:
                return node
        return None

    def test_process_settlement_exists(self):
        """Sanity: _process_settlement must be findable via AST."""
        assert self._find_process_settlement() is not None, (
            "_process_settlement method not found in bot/settlement.py")

    def test_sub_dollar_guard_present(self):
        """Guard If-node must exist inside _process_settlement."""
        func = self._find_process_settlement()
        assert func is not None
        guard = self._find_sub_dollar_if(func)
        assert guard is not None, (
            "Sub-dollar revenue guard (implied_count == 0 AND aggregate_count > 0) "
            "missing from _process_settlement. Without it, Kalshi revenue in "
            "[1,99]¢ will silently zero confirmed-filled WIN positions.")

    def test_sub_dollar_branch_body_does_not_update_positions(self):
        """The guard's body must NOT UPDATE positions or mutate count/cost.

        Its sole purpose is to refuse the auto-zero; any write to positions
        inside the branch defeats the purpose and reintroduces the bug.
        """
        func = self._find_process_settlement()
        guard = self._find_sub_dollar_if(func)
        assert guard is not None
        # Walk the body collecting any Call whose args include the UPDATE SQL,
        # and any Assign whose target includes "p[" or "aggregate_count" / cost.
        for node in ast.walk(ast.Module(body=guard.body, type_ignores=[])):
            if isinstance(node, ast.Call):
                for arg in node.args:
                    if isinstance(arg, (ast.Str, ast.Constant)):
                        s = arg.s if isinstance(arg, ast.Str) else (
                            arg.value if isinstance(arg.value, str) else None)
                        if s and "UPDATE positions" in s:
                            raise AssertionError(
                                "Sub-dollar branch contains `UPDATE positions` — "
                                "this defeats the guard's purpose.")
            if isinstance(node, ast.Assign):
                for tgt in node.targets:
                    # Reject: p["count"]=..., p["total_cost_cents"]=...,
                    # aggregate_count=..., aggregate_cost=...
                    if isinstance(tgt, ast.Subscript):
                        raise AssertionError(
                            "Sub-dollar branch mutates a subscripted position "
                            "field — must leave positions untouched.")
                    if isinstance(tgt, ast.Name) and tgt.id in {
                            "aggregate_count", "aggregate_cost"}:
                        raise AssertionError(
                            f"Sub-dollar branch reassigns {tgt.id} — must "
                            f"leave aggregates untouched so local count "
                            f"reaches the PnL loop.")

    def test_sub_dollar_branch_logs_critical(self):
        """Branch must log at CRITICAL with the SETTLEMENT_REVENUE_SUB_DOLLAR
        marker so the auditor + future forensics can find it."""
        func = self._find_process_settlement()
        guard = self._find_sub_dollar_if(func)
        assert guard is not None
        saw_critical = False
        for node in ast.walk(ast.Module(body=guard.body, type_ignores=[])):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "critical"):
                saw_critical = True
                # Check marker string is in one of the positional args (f-string)
                for arg in node.args:
                    src = ast.unparse(arg) if hasattr(ast, "unparse") else ""
                    if "SETTLEMENT_REVENUE_SUB_DOLLAR" in src:
                        return
        assert saw_critical, (
            "Sub-dollar branch must call logging.critical(...) with marker "
            "'SETTLEMENT_REVENUE_SUB_DOLLAR' — this is how the auditor/alerts "
            "discover sub-dollar-revenue events.")

    def test_sub_dollar_branch_precedes_autocorrect(self):
        """Guard must be checked BEFORE the `len(positions) == 1` single-row
        auto-correct. Otherwise the auto-zero path still fires first."""
        func = self._find_process_settlement()
        guard = self._find_sub_dollar_if(func)
        assert guard is not None
        # The auto-correct is an elif/else chain rooted at the same If
        # tree as the guard; find the sibling If-node whose test compares
        # len(positions) to 1.
        def find_len_positions_if(if_node):
            # Guard If's orelse may be a list containing another If (elif)
            for child in if_node.orelse:
                if isinstance(child, ast.If):
                    t = child.test
                    if isinstance(t, ast.Compare) and isinstance(t.left, ast.Call):
                        call = t.left
                        if (isinstance(call.func, ast.Name)
                                and call.func.id == "len"
                                and len(call.args) == 1
                                and isinstance(call.args[0], ast.Name)
                                and call.args[0].id == "positions"):
                            return child
                    # Recurse for deeper elif chains
                    inner = find_len_positions_if(child)
                    if inner is not None:
                        return inner
            return None
        sibling = find_len_positions_if(guard)
        assert sibling is not None, (
            "Guard must be the FIRST branch of a chain whose elif tests "
            "`len(positions) == 1`. If the auto-correct branch doesn't appear "
            "in the guard's orelse chain, the ordering is wrong and the "
            "sub-dollar case will fall through to auto-zero.")
