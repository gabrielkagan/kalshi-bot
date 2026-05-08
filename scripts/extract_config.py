#!/usr/bin/env python3
"""Extract trading config constants from bot/_impl.py via AST parsing.

Outputs JSON that can feed into doc templates, ensuring docs always
reflect the actual code. No imports of bot/_impl.py — pure static analysis.

Also parses weather_engine.py and sports_data.py for cross-file data.

Usage:
    python3 scripts/extract_config.py > config.json
    python3 scripts/extract_config.py --diff config_previous.json
"""

import ast
import json
import re
import sys
import os
from datetime import datetime, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.join(SCRIPT_DIR, "..")
BOT_PATH = os.path.join(REPO_DIR, "bot/_impl.py")
CONSTANTS_PATH = os.path.join(REPO_DIR, "bot/constants.py")
WEATHER_PATH = os.path.join(REPO_DIR, "weather_engine.py")
SPORTS_PATH = os.path.join(REPO_DIR, "sports_data.py")

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
    "DIRECT_TAKER_THRESHOLD": "Skip maker and go IOC below this many seconds",
    "MIN_EDGE_PCT": "Flat minimum edge fallback",
    "BALANCE_CACHE_TTL": "Balance cache TTL (seconds)",
    "Z_SCORE_MAX": "Z-score rejection threshold",
    "STC_SHADOW_THRESHOLD": "STC shadow threshold (seconds)",
    "PRICE_BUFFER_SIZE": "CoinbaseFeed price buffer size (seconds of 1s snapshots)",

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

    # SPX
    "SPX_HOURLY_OBSERVATION_ONLY": "SPX observation-only mode",
    "SPX_HOURLY_MARKET_BLEND_W": "SPX market blend weight",
    "SPX_HOURLY_MAX_POSITIONS_PER_WINDOW": "Max positions per SPX window",
    "SPX_HOURLY_MAX_WINDOW_RISK": "Max aggregate risk per SPX window",

    # Weather
    "WEATHER_OBSERVATION_ONLY": "Weather observation-only mode",
    "WEATHER_MARKET_BLEND_W": "Weather market blend weight",
    "WEATHER_MIN_ENTRY_PRICE": "Weather minimum entry price (cents)",
    "WEATHER_MAX_RISK_PER_TRADE": "Weather max risk per trade",

    # Calibration
    "CALIBRATION_MIN_SAMPLES_PLATT": "Min samples for Platt calibration",
    "CALIBRATION_MIN_SAMPLES_BETA": "Min samples for Beta calibration",
    "CALIBRATION_MIN_SAMPLES_BLR": "Min samples for BLR calibration",

    # Execution
    "ESCALATION_WAIT_LONG": "Maker wait for long STC",
    "ESCALATION_WAIT_MEDIUM": "Maker wait for medium STC",
    "ESCALATION_WAIT_SHORT": "Maker wait for short STC",

    # Cross-exchange
    "CROSS_EXCHANGE_ENABLED": "Cross-exchange feed enabled",
    "CROSS_EXCHANGE_BUFFER_SIZE": "Cross-exchange price buffer size",
    "CROSS_EXCHANGE_LEAD_THRESHOLD": "Single-exchange lead threshold",
    "CROSS_EXCHANGE_CONSENSUS_THRESHOLD": "Consensus threshold",
    "CROSS_EXCHANGE_CONSENSUS_MIN": "Minimum exchanges for consensus",
    "CROSS_EXCHANGE_STALE_SECONDS": "Cross-exchange stale data timeout",
}


def extract_constants(source: str) -> dict:
    """Parse bot/_impl.py AST and extract module-level constant assignments."""
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

                    # MIN_EDGE_BY_PRICE (list of tuples)
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

                    # DYNAMIC_CAP_SCHEDULE
                    if target.id == "DYNAMIC_CAP_SCHEDULE":
                        try:
                            value = ast.literal_eval(node.value)
                            extras["DYNAMIC_CAP_SCHEDULE"] = {
                                "value": value,
                                "line": node.lineno,
                                "description": "Dynamic probability cap schedule (STC, cap)",
                            }
                        except (ValueError, TypeError):
                            pass

                    # HOURLY_DYNAMIC_CAP_SCHEDULE
                    if target.id == "HOURLY_DYNAMIC_CAP_SCHEDULE":
                        try:
                            value = ast.literal_eval(node.value)
                            extras["HOURLY_DYNAMIC_CAP_SCHEDULE"] = {
                                "value": value,
                                "line": node.lineno,
                                "description": "Hourly dynamic probability cap schedule",
                            }
                        except (ValueError, TypeError):
                            pass

                    # CROSS_EXCHANGE_SYMBOLS
                    if target.id == "CROSS_EXCHANGE_SYMBOLS":
                        try:
                            value = ast.literal_eval(node.value)
                            extras["CROSS_EXCHANGE_SYMBOLS"] = {
                                "value": value,
                                "line": node.lineno,
                                "description": "Cross-exchange symbol mapping",
                            }
                        except (ValueError, TypeError):
                            pass

    return extras


def extract_exchange_feeds(source: str) -> list:
    """Extract exchange feed class names from bot/_impl.py (CoinbaseFeed, etc.)."""
    tree = ast.parse(source)
    feeds = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            if "Feed" in node.name or "WebSocket" in node.name:
                feeds.append(node.name)
    return sorted(feeds)


def extract_weather_cities() -> dict:
    """Parse weather_engine.py for WEATHER_CITIES dict via regex.

    Returns dict with city_count, city_codes, city_names, city_tickers.
    Uses regex instead of AST because WEATHER_CITIES uses typed Dict annotation.
    """
    if not os.path.exists(WEATHER_PATH):
        return {}

    with open(WEATHER_PATH) as f:
        content = f.read()

    cities = {}
    # Parse each city entry: "CODE": { "name": "City Name", ... "series_ticker": "KXHIGH..." }
    city_pattern = re.compile(
        r'"([A-Z]{2,4})":\s*\{\s*'
        r'"name":\s*"([^"]+)".*?'
        r'"series_ticker":\s*"([^"]+)"',
        re.DOTALL
    )
    for m in city_pattern.finditer(content):
        code, name, ticker = m.groups()
        cities[code] = {"name": name, "series_ticker": ticker}

    if not cities:
        return {}

    return {
        "weather_city_count": len(cities),
        "weather_city_codes": sorted(cities.keys()),
        "weather_city_names": [cities[k]["name"] for k in sorted(cities.keys())],
        "weather_city_tickers": [cities[k]["series_ticker"] for k in sorted(cities.keys())],
    }


def extract_sports_leagues() -> dict:
    """Parse sports_data.py for LEAGUES dict via regex.

    Returns dict with league_count, league_tickers, league_names.
    Uses regex instead of AST because LEAGUES uses LeagueConfig() calls.
    """
    if not os.path.exists(SPORTS_PATH):
        return {}

    with open(SPORTS_PATH) as f:
        content = f.read()

    leagues = {}
    # Parse: "KXTICKER": LeagueConfig(..., display_name="Name", ...)
    league_pattern = re.compile(
        r'"(KX\w+)":\s*LeagueConfig\([^)]*?display_name="([^"]+)"',
        re.DOTALL
    )
    for m in league_pattern.finditer(content):
        ticker, display_name = m.groups()
        leagues[ticker] = display_name

    if not leagues:
        return {}

    return {
        "sports_league_count": len(leagues),
        "sports_league_tickers": sorted(leagues.keys()),
        "sports_league_names": [leagues[k] for k in sorted(leagues.keys())],
    }


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
        print(json.dumps({"error": f"bot/_impl.py not found at {BOT_PATH}"}))
        sys.exit(1)

    with open(BOT_PATH) as f:
        bot_source = f.read()

    # Bit 3.1: module-level constants live in bot/constants.py. Concatenate
    # both files so extract_constants() / extract_compound_constants()
    # find every TRACKED_CONSTANTS entry regardless of which file it lives
    # in. extract_exchange_feeds() (class-scan) keeps its bot_source-only
    # input — classes don't move.
    constants_source = ""
    if os.path.exists(CONSTANTS_PATH):
        with open(CONSTANTS_PATH) as f:
            constants_source = f.read()
    combined_source = bot_source + "\n" + constants_source

    line_count = len(bot_source.splitlines())
    constants = extract_constants(combined_source)
    compounds = extract_compound_constants(combined_source)
    constants.update(compounds)

    # Extract exchange feed classes (still bot/_impl.py only — classes stay)
    exchange_feeds = extract_exchange_feeds(bot_source)

    # Cross-file extractions
    weather_data = extract_weather_cities()
    sports_data = extract_sports_leagues()

    output = {
        "constants": constants,
        "_bot_lines": line_count,
        "_exchange_feeds": exchange_feeds,
        "_extracted_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "_bot_path": os.path.abspath(BOT_PATH),
    }

    # Merge cross-file data into top-level (not under constants — these aren't bot/_impl.py constants)
    if weather_data:
        output["_weather"] = weather_data
    if sports_data:
        output["_sports"] = sports_data

    # Derive exchange list from CROSS_EXCHANGE_SYMBOLS
    cross_syms = constants.get("CROSS_EXCHANGE_SYMBOLS", {}).get("value")
    if cross_syms and isinstance(cross_syms, dict):
        # Get exchange names from the first asset's symbol mapping
        first_asset = next(iter(cross_syms.values()), {})
        if isinstance(first_asset, dict):
            output["_exchange_names"] = ["Coinbase"] + [
                ex.title() for ex in sorted(first_asset.keys())
            ]

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
