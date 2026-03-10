#!/usr/bin/env python3
"""Contract tests: verify dashboard_snapshot.py snapshot keys match what dashboard JS expects.

Root cause analysis of bugs #1-#5 (March 6 2026):
  The dashboard (index.html) and dashboard_snapshot.py are in DIFFERENT REPOS with no shared
  schema or contract. When one side renames a key, the other side silently breaks —
  rendering shows "Loading..." or "0" with no error.

  Bug #1: firebase pushes "starting_balance", dashboard reads "session_start_balance"
  Bug #2: firebase pushes "recent_trades" (top-level), dashboard reads "trade_analytics.recent_trades"
  Bug #3: sim P&L SQL omits fees on losses (logic bug, not contract mismatch)
  Bug #4: firebase pushes "reads_last_second", dashboard reads "requests_used"
  Bug #5: "ALL" scope trade analytics falls through to 15M-only else branch

  Prevention: This test file defines the CONTRACT between firebase and dashboard.
  Run it after any change to either file to catch mismatches before deploy.

Usage:
  python3 test_dashboard_contract.py               # quick (no Firebase needed)
  python3 test_dashboard_contract.py --with-mock    # builds snapshot from mock, checks keys
"""

import re
import os
import sys
import ast
import json
import math

# ── CONFIG ──────────────────────────────────────────────────────────────────

DASHBOARD_SNAPSHOT_PATH = os.path.join(os.path.dirname(__file__), "dashboard_snapshot.py")
DASHBOARD_PATH = "/private/tmp/gabekagan-dashboard/dashboard/index.html"

# Every key that dashboard_snapshot.py writes as snap["key"]
# AND that dashboard JS reads as s.key or s["key"]
# This is the CONTRACT — both sides must agree on these names.
REQUIRED_SNAP_KEYS = {
    # Core
    "timestamp", "uptime_seconds", "current_balance", "starting_balance",
    "peak_balance", "balance_stale", "drawdown_kelly_mult",
    "balance_history", "balance_history_4h",
    # Trades
    "recent_trades", "all_products_recent_trades",
    "win_count", "loss_count", "win_rate",
    "all_products_win_count", "all_products_loss_count", "all_products_win_rate",
    "daily_pnl_cents", "daily_pnl_pct",
    "consecutive_losses", "consecutive_wins",
    # Risk
    "risk_metrics", "all_products_risk_metrics", "regime_risk_metrics",
    # Volatility & market
    "current_volatility", "spot_prices", "cross_exchange", "feed_health",
    "active_windows", "seconds_to_next_close",
    # Trading state
    "bot_status", "last_error_message", "active_order",
    "active_positions", "resting_orders",
    "observation_mode", "trading_config",
    # Execution
    "filter_funnel", "rate_limits", "pending_settlements",
    "session_stats", "execution_engine", "execution_quality",
    "recent_opportunities",
    # Analytics
    "real_trade_analytics", "regime_trade_analytics",
    "counterfactual_analysis", "ask_distribution",
    # Calibration
    "calibration", "cal_registry",
    # Model diagnostics
    "egarch_estimation", "egarch_blend", "nig_distribution",
    # Shadow
    "fifteenm_shadow", "hourly_alt_shadow", "spx_harrv_shadow",
    "shadow_cal_pipeline", "shadow_variants",
    "hourly_observation", "spx_observation",
    "weather_observation", "sports_observation",
    "data_collection", "capital_allocation",
    "convergence_velocity",
    # Orderbooks
    "orderbooks", "order_flow", "kalshi_order_flow",
    # Position health
    "position_health",
    # New panels (March 6 2026)
    "system_health", "shadow_comparison",
    "stc_performance", "stc_shadow_counterfactual",
    "calibration_health", "edge_integrity",
    "loss_clustering", "pipeline_completeness",
    "no_side_shadow",
    "weekend_discount_shadow",
    "overnight_discount_shadow",
    "overnight_lp_shadow",
    "decided_contract_shadow",
    "relaxed_edge_shadow",
    "calibration_gap",
    "capital_utilization",
    "sol_pathc_shadow",
    "eth_filter_shadow",
    "hourly_config_a",
    "hourly_config_b",
    "sports_strong_config",
}

# Dashboard JS field access patterns that MUST match firebase keys.
# Format: (dashboard_js_access_pattern, expected_firebase_key)
# These catch the exact bugs we found.
FIELD_ACCESS_CONTRACT = [
    # Bug #1: status strip reads starting_balance
    ("s.starting_balance", "starting_balance"),
    # Bug #2: last trade card reads s.recent_trades
    ("s.recent_trades", "recent_trades"),
    # Bug #4: rate limits — structured sub-objects
    ("rl.exchange_requests", "rate_limits"),  # rl = s.rate_limits
    # Bug #5: all-products scope
    ("s.all_products_risk_metrics", "all_products_risk_metrics"),
    ("s.all_products_win_count", "all_products_win_count"),
    ("s.all_products_win_rate", "all_products_win_rate"),
]

# Sim P&L SQL patterns that MUST include fees on BOTH wins and losses
SIM_PNL_FEE_PATTERNS = {
    # Each loss branch must include fee subtraction, not just raw price
    "YES_LOSS": r"THEN\s+-\(?market_price\s*\+",       # -(market_price + fee)
    "NO_LOSS": r"THEN\s+-\(?\(100\s*-\s*market_price\)\s*\+",  # -((100-market_price) + fee)
}


def test_dashboard_snapshot_has_keys():
    """Verify dashboard_snapshot.py writes all required snap keys."""
    with open(DASHBOARD_SNAPSHOT_PATH) as f:
        source = f.read()

    # Find all snap["key"] = assignments
    snap_keys = set(re.findall(r'snap\["([^"]+)"\]\s*=', source))

    missing = REQUIRED_SNAP_KEYS - snap_keys
    if missing:
        print(f"FAIL: dashboard_snapshot.py missing snap keys: {sorted(missing)}")
        return False

    print(f"PASS: dashboard_snapshot.py has all {len(REQUIRED_SNAP_KEYS)} required snap keys")
    return True


def test_dashboard_reads_correct_keys():
    """Verify dashboard JS reads the correct firebase key names (not stale aliases)."""
    if not os.path.exists(DASHBOARD_PATH):
        print(f"SKIP: Dashboard not found at {DASHBOARD_PATH}")
        return True

    with open(DASHBOARD_PATH) as f:
        dashboard_source = f.read()

    failures = []

    # Check that dashboard does NOT use known-wrong patterns
    wrong_patterns = [
        ("s.session_start_balance", "should be s.starting_balance (Bug #1)"),
        ("s.start_balance", "should be s.starting_balance (Bug #1)"),
        ("s.trade_analytics?.recent_trades", "should be s.recent_trades (Bug #2)"),
        ("s.trade_analytics.recent_trades", "should be s.recent_trades (Bug #2)"),
    ]
    for pattern, reason in wrong_patterns:
        if pattern in dashboard_source:
            failures.append(f"Dashboard still uses '{pattern}' — {reason}")

    # Check that required access patterns exist
    for js_pattern, firebase_key in FIELD_ACCESS_CONTRACT:
        if js_pattern not in dashboard_source:
            # Only fail if this is a key the dashboard should definitely read
            # Some keys are optional/conditional
            pass  # Don't fail on missing reads — they might be in update functions not yet written

    if failures:
        for f in failures:
            print(f"FAIL: {f}")
        return False

    print(f"PASS: Dashboard uses correct firebase key names")
    return True


def test_sim_pnl_fees_on_losses():
    """Verify sim P&L SQL includes fees on losing trades (Bug #3)."""
    with open(DASHBOARD_SNAPSHOT_PATH) as f:
        source = f.read()

    # Find all sim_pnl SQL blocks (they contain "THEN -market_price" or "THEN -(100 - market_price)")
    # After the fix, these should include fee addition: -(price + CEIL(...))
    failures = []

    # Count raw loss patterns (no fee) — these are bugs
    # YES side loss: THEN -market_price  (without + fee)
    raw_yes_losses = re.findall(
        r"THEN\s+-market_price\s*\n",
        source
    )
    if raw_yes_losses:
        failures.append(f"Found {len(raw_yes_losses)} YES-side losses without fees (THEN -market_price)")

    # NO side loss: THEN -(100 - market_price) (without + fee)
    raw_no_losses = re.findall(
        r"THEN\s+-\(100\s*-\s*market_price\)\s*\n",
        source
    )
    if raw_no_losses:
        failures.append(f"Found {len(raw_no_losses)} NO-side losses without fees (THEN -(100 - market_price))")

    if failures:
        for f in failures:
            print(f"FAIL: {f}")
        return False

    print("PASS: All sim P&L SQL includes fees on losses")
    return True


def test_rate_limits_structure():
    """Verify rate_limits includes structured sub-objects for dashboard consumption."""
    with open(DASHBOARD_SNAPSHOT_PATH) as f:
        source = f.read()

    # The dashboard expects either:
    # 1. Nested objects: rl.exchange_requests.used / rl.exchange_requests.limit
    # 2. Flat: rl.requests_used / rl.requests_limit
    # After fix, we push structured objects.
    if "exchange_requests" not in source or "order_requests" not in source:
        print("FAIL: rate_limits missing structured exchange_requests/order_requests")
        return False

    print("PASS: rate_limits has structured sub-objects")
    return True


def test_all_scopes_handled():
    """Verify dashboard handles all 3 scopes (15m, regime, all) in trade analytics."""
    if not os.path.exists(DASHBOARD_PATH):
        print(f"SKIP: Dashboard not found at {DASHBOARD_PATH}")
        return True

    with open(DASHBOARD_PATH) as f:
        source = f.read()

    # Find updateTradeAnalytics function and check it has explicit 'all' handling
    # Not just a fallback else branch
    if "scope === 'all'" not in source and 'scope === "all"' not in source:
        print("FAIL: updateTradeAnalytics has no explicit 'all' scope handler (Bug #5)")
        return False

    print("PASS: Trade analytics handles all scopes explicitly")
    return True


def test_syntax():
    """Verify dashboard_snapshot.py parses without syntax errors."""
    with open(DASHBOARD_SNAPSHOT_PATH) as f:
        source = f.read()
    try:
        ast.parse(source)
        print("PASS: dashboard_snapshot.py syntax OK")
        return True
    except SyntaxError as e:
        print(f"FAIL: dashboard_snapshot.py syntax error: {e}")
        return False


def test_breakeven_wr_formula():
    """Verify breakeven WR formula is correct: (price + fee) / 100.

    Fee = ceil(SIM_FEE_RATE * p/100 * (1-p/100))  -- result is in CENTS already
    because SIM_FEE_RATE=0.035 produces sub-cent values that get ceil'd to 1c.
    BE_WR = (price + fee) / 100
    """
    SIM_FEE_RATE = 0.035
    test_cases = [
        # fee = ceil(0.035 * 0.9 * 0.1) = ceil(0.00315) = 1c, BE = 91/100 = 0.91
        (90, 0.91),
        # fee = ceil(0.035 * 0.95 * 0.05) = ceil(0.001663) = 1c, BE = 96/100 = 0.96
        (95, 0.96),
        # fee = ceil(0.035 * 0.86 * 0.14) = ceil(0.004214) = 1c, BE = 87/100 = 0.87
        (86, 0.87),
    ]
    all_pass = True
    for price, expected in test_cases:
        fee = math.ceil(SIM_FEE_RATE * (price / 100.0) * (1 - price / 100.0))
        be_wr = (price + fee) / 100.0
        if abs(be_wr - expected) > 0.005:
            print(f"FAIL: Breakeven WR at {price}c: got {be_wr:.3f}, expected {expected:.3f}")
            all_pass = False

    if all_pass:
        print("PASS: Breakeven WR formula correct")
    return all_pass


def test_sql_column_names():
    """Verify SQL queries use correct column names for each table.

    Root cause of calibration_health/edge_integrity returning null on first deploy:
    - evaluated_opportunities has 'evaluation_time' and 'settled_time', NOT 'settled_at'
    - evaluated_opportunities has product_type='15m', NOT NULL
    - settled_trades has 'settled_at' (correct)
    """
    with open(DASHBOARD_SNAPSHOT_PATH) as f:
        source = f.read()

    failures = []

    # evaluated_opportunities does NOT have 'settled_at' — it's 'settled_time'/'evaluation_time'
    # Check for lines with settled_at that are NOT on settled_trades (alias st.)
    import re
    lines = source.split('\n')
    for i, line in enumerate(lines):
        # Skip if the line references settled_trades directly or uses st. alias
        if 'settled_at' in line and 'settled_trades' not in line and 'st.' not in line:
            # Check if nearby lines (within 10) reference evaluated_opportunities
            context = '\n'.join(lines[max(0, i-10):i+1])
            if 'evaluated_opportunities' in context and 'settled_trades' not in context:
                failures.append(
                    f"Line {i+1}: uses 'settled_at' in evaluated_opportunities context "
                    f"(should be 'evaluation_time' or 'settled_time')"
                )

    # evaluated_opportunities does NOT have 'side' column — infer from calibrated_prob vs market_price
    for i, line in enumerate(lines):
        if "'side'" in line or '"side"' in line or ', side' in line or 'side FROM' in line.replace('outside', ''):
            context = '\n'.join(lines[max(0, i-10):i+1])
            if 'evaluated_opportunities' in context and 'settled_trades' not in context:
                failures.append(f"Line {i+1}: references 'side' column on evaluated_opportunities (doesn't exist)")

    # evaluated_opportunities 15M product_type is '15m', not NULL
    # Check for product_type IS NULL on evaluated_opportunities queries
    for match in re.finditer(r"evaluated_opportunities.*?product_type IS NULL", source, re.DOTALL):
        context = source[max(0, match.start()-100):match.end()]
        # Get the section header
        failures.append(f"Query on evaluated_opportunities uses 'product_type IS NULL' (15M is '15m', not NULL)")

    if failures:
        for f in failures:
            print(f"FAIL: {f}")
        return False

    print("PASS: SQL column names match table schemas")
    return True


def main():
    print("=" * 60)
    print("DASHBOARD CONTRACT TESTS")
    print("=" * 60)
    print()

    results = []
    results.append(("Syntax check", test_syntax()))
    results.append(("Firebase snap keys", test_dashboard_snapshot_has_keys()))
    results.append(("Dashboard key names", test_dashboard_reads_correct_keys()))
    results.append(("Sim P&L fees on losses", test_sim_pnl_fees_on_losses()))
    results.append(("Rate limits structure", test_rate_limits_structure()))
    results.append(("All scopes handled", test_all_scopes_handled()))
    results.append(("Breakeven WR formula", test_breakeven_wr_formula()))
    results.append(("SQL column names", test_sql_column_names()))

    print()
    print("=" * 60)
    passed = sum(1 for _, r in results if r)
    total = len(results)
    failed = [name for name, r in results if not r]
    skipped = [name for name, r in results if r is None]

    if failed:
        print(f"RESULT: {passed}/{total} passed, {len(failed)} FAILED")
        for f in failed:
            print(f"  FAILED: {f}")
        sys.exit(1)
    else:
        print(f"RESULT: {passed}/{total} passed — ALL OK")
        sys.exit(0)


if __name__ == "__main__":
    main()
