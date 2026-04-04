"""Snapshot / Contract Tests.

Failure mode: Dashboard, Supabase sync, or analyst.py expect a specific data shape
that bot.py silently changes.

Wraps the existing test_dashboard_contract.py tests into pytest format, and adds
additional schema stability tests.
"""

import ast
import math
import os
import re
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

DASHBOARD_SNAPSHOT_PATH = os.path.join(PROJECT_ROOT, "dashboard_snapshot.py")
DASHBOARD_PATH = "/private/tmp/gabekagan-dash/dashboard/index.html"

# The contract: every key dashboard_snapshot.py writes as snap["key"]
REQUIRED_SNAP_KEYS = {
    "timestamp", "uptime_seconds", "current_balance", "starting_balance",
    "peak_balance", "balance_stale", "drawdown_kelly_mult",
    "balance_history", "balance_history_4h",
    "recent_trades", "all_products_recent_trades",
    "win_count", "loss_count", "win_rate",
    "all_products_win_count", "all_products_loss_count", "all_products_win_rate",
    "daily_pnl_cents", "daily_pnl_pct",
    "consecutive_losses", "consecutive_wins",
    "risk_metrics", "all_products_risk_metrics", "regime_risk_metrics",
    "current_volatility", "spot_prices", "cross_exchange", "feed_health",
    "active_windows", "seconds_to_next_close",
    "bot_status", "last_error_message", "active_order",
    "active_positions", "resting_orders",
    "observation_mode", "trading_config",
    "filter_funnel", "rate_limits", "pending_settlements",
    "session_stats", "execution_engine", "execution_quality",
    "recent_opportunities",
    "real_trade_analytics", "regime_trade_analytics",
    "counterfactual_analysis", "ask_distribution",
    "calibration", "cal_registry",
    "egarch_estimation", "egarch_blend", "nig_distribution",
    "fifteenm_shadow", "hourly_alt_shadow", "spx_harrv_shadow",
    "shadow_cal_pipeline", "shadow_variants",
    "hourly_observation", "spx_observation",
    "weather_observation", "sports_observation",
    "data_collection", "capital_allocation",
    "convergence_velocity",
    "orderbooks", "order_flow", "kalshi_order_flow",
    "position_health",
    "system_health", "shadow_comparison",
    "stc_performance", "stc_shadow_counterfactual",
    "calibration_health", "edge_integrity",
    "loss_clustering", "pipeline_completeness",
    "no_side_shadow",
}


class TestDashboardSnapshotKeys:
    """dashboard_snapshot.py writes all required snap keys."""

    def test_snapshot_has_all_required_keys(self):
        if not os.path.exists(DASHBOARD_SNAPSHOT_PATH):
            pytest.skip("dashboard_snapshot.py not found")

        with open(DASHBOARD_SNAPSHOT_PATH) as f:
            source = f.read()

        snap_keys = set(re.findall(r'snap\["([^"]+)"\]\s*=', source))
        missing = REQUIRED_SNAP_KEYS - snap_keys
        assert not missing, f"dashboard_snapshot.py missing snap keys: {sorted(missing)}"

    def test_snapshot_syntax(self):
        if not os.path.exists(DASHBOARD_SNAPSHOT_PATH):
            pytest.skip("dashboard_snapshot.py not found")

        with open(DASHBOARD_SNAPSHOT_PATH) as f:
            source = f.read()
        ast.parse(source)


class TestDashboardKeyNames:
    """Dashboard JS reads correct key names (not stale aliases)."""

    @pytest.fixture
    def dashboard_source(self):
        if not os.path.exists(DASHBOARD_PATH):
            pytest.skip("Dashboard not found")
        with open(DASHBOARD_PATH) as f:
            return f.read()

    def test_no_stale_starting_balance(self, dashboard_source):
        """Bug #1: dashboard should use s.starting_balance, not s.session_start_balance."""
        assert "s.session_start_balance" not in dashboard_source
        assert "s.start_balance" not in dashboard_source

    def test_no_stale_trade_analytics_recent_trades(self, dashboard_source):
        """Bug #2: should use s.recent_trades, not s.trade_analytics.recent_trades."""
        assert "s.trade_analytics?.recent_trades" not in dashboard_source
        assert "s.trade_analytics.recent_trades" not in dashboard_source

    def test_all_scope_handled(self, dashboard_source):
        """Bug #5: trade analytics must explicitly handle 'all' scope."""
        assert ("scope === 'all'" in dashboard_source or
                'scope === "all"' in dashboard_source)


class TestRateLimitsStructure:
    """rate_limits includes structured sub-objects for dashboard consumption."""

    def test_rate_limits_has_structured_keys(self):
        if not os.path.exists(DASHBOARD_SNAPSHOT_PATH):
            pytest.skip("dashboard_snapshot.py not found")

        with open(DASHBOARD_SNAPSHOT_PATH) as f:
            source = f.read()

        assert "exchange_requests" in source, "rate_limits missing exchange_requests"
        assert "order_requests" in source, "rate_limits missing order_requests"


class TestSimPnLFees:
    """Sim P&L SQL includes fees on losing trades (Bug #3)."""

    def test_no_raw_loss_without_fees(self):
        if not os.path.exists(DASHBOARD_SNAPSHOT_PATH):
            pytest.skip("dashboard_snapshot.py not found")

        with open(DASHBOARD_SNAPSHOT_PATH) as f:
            source = f.read()

        raw_yes_losses = re.findall(r"THEN\s+-market_price\s*\n", source)
        assert not raw_yes_losses, (
            f"Found {len(raw_yes_losses)} YES-side losses without fees")

        raw_no_losses = re.findall(r"THEN\s+-\(100\s*-\s*market_price\)\s*\n", source)
        assert not raw_no_losses, (
            f"Found {len(raw_no_losses)} NO-side losses without fees")


class TestBreakevenWRFormula:
    """Breakeven WR formula: (price + fee) / 100."""

    def test_breakeven_wr_at_known_prices(self):
        SIM_FEE_RATE = 0.035
        test_cases = [
            (90, 0.91),
            (95, 0.96),
            (86, 0.87),
        ]
        for price, expected in test_cases:
            fee = math.ceil(SIM_FEE_RATE * (price / 100.0) * (1 - price / 100.0))
            be_wr = (price + fee) / 100.0
            assert abs(be_wr - expected) < 0.005, (
                f"Breakeven WR at {price}c: got {be_wr:.3f}, expected {expected:.3f}")


class TestSQLColumnNames:
    """SQL queries use correct column names for each table."""

    def test_no_settled_at_on_evaluated_opportunities(self):
        """evaluated_opportunities has evaluation_time/settled_time, NOT settled_at."""
        if not os.path.exists(DASHBOARD_SNAPSHOT_PATH):
            pytest.skip("dashboard_snapshot.py not found")

        with open(DASHBOARD_SNAPSHOT_PATH) as f:
            source = f.read()

        lines = source.split('\n')
        failures = []
        for i, line in enumerate(lines):
            if 'settled_at' in line and 'settled_trades' not in line and 'st.' not in line:
                context = '\n'.join(lines[max(0, i - 10):i + 1])
                if 'evaluated_opportunities' in context and 'settled_trades' not in context:
                    failures.append(f"Line {i+1}: 'settled_at' in evaluated_opportunities context")

        assert not failures, f"Wrong column name: {failures}"
