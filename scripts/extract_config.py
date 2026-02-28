#!/usr/bin/env python3
"""Extract trading config constants from bot.py via AST parsing.

Outputs JSON that can feed into doc templates, ensuring docs always
reflect the actual code. No imports of bot.py — pure static analysis.

Usage:
    python3 scripts/extract_config.py > config.json
    python3 scripts/extract_config.py --diff config_previous.json
"""

import ast
import json
import sys
import os
from datetime import datetime, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BOT_PATH = os.path.join(SCRIPT_DIR, "..", "bot.py")

# Constants to extract (name -> human-readable description)
TRACKED_CONSTANTS = {
    # Core trading
    "OBSERVATION_MODE": "Live vs observation mode",
    "MIN_ENTRY_PRICE": "Minimum entry price (cents)",
    "MAX_ENTRY_PRICE": "Maximum entry price (cents)",
    "MAX_SECONDS_BEFORE_CLOSE": "Entry window (seconds before close)",
    "MIN_SECONDS_BEFORE_CLOSE": "Minimum seconds before close",
    "MARKET_BLEND_W": "Market blend weight (0=pure model, 1=pure market)",
    "MAX_RISK_PER_TRADE": "Max bankroll fraction per trade",
    "MAKER_ONLY_THRESHOLD": "No taker execution below this many seconds",
    "MIN_EDGE_PCT": "Flat minimum edge fallback",
    "BALANCE_CACHE_TTL": "Balance cache TTL (seconds)",
    "Z_SCORE_MAX": "Z-score rejection threshold",

    # Drawdown
    "DRAWDOWN_HALF_THRESHOLD": "Halve sizing below this ratio",
    "DRAWDOWN_QUARTER_THRESHOLD": "Quarter sizing below this ratio",
    "DRAWDOWN_HALT_THRESHOLD": "Stop trading below this ratio",

    # Hourly
    "HOURLY_OBSERVATION_ONLY": "Hourly observation-only mode",
    "HOURLY_MARKET_BLEND_W": "Hourly market blend weight",
    "HOURLY_MIN_ENTRY_PRICE": "Hourly minimum entry price (cents)",
    "HOURLY_MAX_RISK_PER_TRADE": "Hourly max risk per trade",
    "HOURLY_TEMPERATURE_T": "Hourly temperature scaling factor",
    "HOURLY_KELLY_FRACTION": "Hourly Kelly fraction",
    "HOURLY_MIN_STC_ENTRY": "Hourly min STC for entry (seconds)",
    "HOURLY_MAX_STC_ENTRY": "Hourly max STC for entry (seconds)",
    "HOURLY_MAX_POSITIONS_PER_WINDOW": "Max positions per hourly window",
    "HOURLY_MAX_WINDOW_RISK": "Max aggregate risk per hourly window",
    "HOURLY_MAX_SECONDS_BEFORE_CLOSE": "Hourly entry window (seconds)",

    # Shadow modes
    "EGARCH_SHADOW_MODE": "EGARCH shadow mode",
    "EGARCH_BLEND_SHADOW_MODE": "EGARCH blend shadow mode",
    "RK_TV_SHADOW_MODE": "TV RK weights shadow mode",
    "SHADOW_CAL_PIPELINE": "Cal pipeline shadow mode",
    "KALSHI_OFT_SHADOW_MODE": "Kalshi OFT shadow mode",
    "MZ_SIGMOID_SHADOW_MODE": "Sigmoid QLIKE shadow mode",
    "JUMP_ADAPTIVE": "Adaptive jump detection enabled",
    "RK_ADAPTIVE": "Adaptive RK bandwidth enabled",

    # Calibration
    "CALIBRATION_MIN_SAMPLES_BLR": "Min samples for BLR calibration",
}


def extract_constants(source: str) -> dict:
    """Parse bot.py AST and extract module-level constant assignments."""
    tree = ast.parse(source)
    constants = {}

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in TRACKED_CONSTANTS:
                    try:
                        value = ast.literal_eval(node.value)
                        constants[target.id] = {
                            "value": value,
                            "line": node.lineno,
                            "description": TRACKED_CONSTANTS[target.id],
                        }
                    except (ValueError, TypeError):
                        # Can't literal_eval (e.g., set() or function call)
                        constants[target.id] = {
                            "value": ast.dump(node.value),
                            "line": node.lineno,
                            "description": TRACKED_CONSTANTS[target.id],
                            "_raw": True,
                        }

    return constants


def extract_compound_constants(source: str) -> dict:
    """Extract constants that need special handling (lists, sets, dicts)."""
    extras = {}
    tree = ast.parse(source)

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    # SIZING_TIERS
                    if target.id == "SIZING_TIERS":
                        try:
                            value = ast.literal_eval(node.value)
                            extras["SIZING_TIERS"] = {
                                "value": value,
                                "line": node.lineno,
                                "description": "Edge-tiered sizing schedule",
                            }
                        except (ValueError, TypeError):
                            pass

                    # MIN_EDGE_BY_PRICE (dict)
                    if target.id == "MIN_EDGE_BY_PRICE":
                        try:
                            value = ast.literal_eval(node.value)
                            extras["MIN_EDGE_BY_PRICE"] = {
                                "value": value,
                                "line": node.lineno,
                                "description": "Price-dependent edge schedule",
                            }
                        except (ValueError, TypeError):
                            pass

                    # HOURLY_EXCLUDED_ASSETS (set)
                    if target.id == "HOURLY_EXCLUDED_ASSETS":
                        try:
                            value = ast.literal_eval(node.value)
                            if isinstance(value, set):
                                value = sorted(value)
                            extras["HOURLY_EXCLUDED_ASSETS"] = {
                                "value": value,
                                "line": node.lineno,
                                "description": "Assets excluded from hourly trading",
                            }
                        except (ValueError, TypeError):
                            pass

    return extras


def diff_configs(current: dict, previous_path: str) -> list:
    """Compare current config against a previous config.json and return changes."""
    with open(previous_path) as f:
        previous = json.load(f)

    prev_consts = previous.get("constants", {})
    curr_consts = current.get("constants", {})
    changes = []

    all_keys = set(list(prev_consts.keys()) + list(curr_consts.keys()))
    for key in sorted(all_keys):
        old_val = prev_consts.get(key, {}).get("value")
        new_val = curr_consts.get(key, {}).get("value")
        if old_val != new_val:
            changes.append({
                "constant": key,
                "old": old_val,
                "new": new_val,
                "description": curr_consts.get(key, prev_consts.get(key, {})).get("description", ""),
            })

    return changes


def main():
    if not os.path.exists(BOT_PATH):
        print(json.dumps({"error": f"bot.py not found at {BOT_PATH}"}))
        sys.exit(1)

    with open(BOT_PATH) as f:
        source = f.read()

    line_count = len(source.splitlines())
    constants = extract_constants(source)
    compounds = extract_compound_constants(source)
    constants.update(compounds)

    output = {
        "constants": constants,
        "_bot_lines": line_count,
        "_extracted_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "_bot_path": os.path.abspath(BOT_PATH),
    }

    # Diff mode
    if len(sys.argv) > 2 and sys.argv[1] == "--diff":
        changes = diff_configs(output, sys.argv[2])
        if changes:
            print("Config changes detected:", file=sys.stderr)
            for c in changes:
                print(f"  {c['constant']}: {c['old']} -> {c['new']}", file=sys.stderr)
        output["_changes"] = changes

    print(json.dumps(output, indent=2, default=str))


if __name__ == "__main__":
    main()
