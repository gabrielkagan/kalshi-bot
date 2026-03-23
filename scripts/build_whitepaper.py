#!/usr/bin/env python3
"""Render whitepaper.md and README.template.md by replacing {{PLACEHOLDER}} markers with live stats.

Consumes:
  - whitepaper_stats.json (from generate_whitepaper_stats.py on VPS)
  - config.json (from extract_config.py, AST-parsed from bot.py + cross-file)

Produces:
  - whitepaper_rendered.md
  - whitepaper_investor_rendered.md
  - README.md
"""

import json
import os
import re
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.join(SCRIPT_DIR, "..")
TEMPLATE_PATH = os.path.join(REPO_DIR, "whitepaper.md")
STATS_PATH = os.path.join(REPO_DIR, "whitepaper_stats.json")
CONFIG_PATH = os.path.join(REPO_DIR, "config.json")
OUTPUT_PATH = os.path.join(REPO_DIR, "whitepaper_rendered.md")
INVESTOR_TEMPLATE_PATH = os.path.join(REPO_DIR, "whitepaper_investor.md")
INVESTOR_OUTPUT_PATH = os.path.join(REPO_DIR, "whitepaper_investor_rendered.md")
README_TEMPLATE_PATH = os.path.join(REPO_DIR, "README.template.md")
README_OUTPUT_PATH = os.path.join(REPO_DIR, "README.md")


def pct(n, total):
    """Format a percentage string."""
    if total == 0:
        return "0%"
    return f"{n / total * 100:.1f}\\%"


def rate(wins, n):
    """Format a win rate string."""
    if n == 0:
        return "---"
    return f"{wins / n * 100:.1f}\\%"


def _get_const_value(config, key, default=None):
    """Safely get a constant value from config.json."""
    return config.get("constants", {}).get(key, {}).get("value", default)


def _build_sizing_tiers_table(tiers):
    """Generate a markdown table from SIZING_TIERS list of (min_edge, risk_fraction) tuples."""
    if not tiers:
        return "*Sizing tiers not available*"
    lines = [
        "| Min Fee-Adj Edge | Risk Fraction |",
        "|:-----------------|:-------------|",
    ]
    for min_edge, risk_frac in tiers:
        lines.append(f"| \u2265 {min_edge * 100:.2g}% | {risk_frac * 100:.0f}% |")
    return "\n".join(lines)


def _build_edge_schedule_table(schedule):
    """Generate a markdown table from MIN_EDGE_BY_PRICE list of (price_floor, min_edge) tuples."""
    if not schedule:
        return "*Edge schedule not available*"
    lines = [
        "| Price Range | Min Edge |",
        "|:-----------|:---------|",
    ]
    # Schedule is sorted descending by price_floor: [(97, 0.02), (95, 0.0125), ...]
    for i, (floor, edge) in enumerate(schedule):
        if i == 0:
            range_str = f"{floor}--99c"
        else:
            prev_floor = schedule[i - 1][0]
            range_str = f"{floor}--{prev_floor - 1}c"
        lines.append(f"| {range_str} | {edge * 100:.2g}% |")
    return "\n".join(lines)


def _build_drawdown_table(config):
    """Generate a markdown table from drawdown thresholds."""
    half = _get_const_value(config, "DRAWDOWN_HALF_THRESHOLD")
    quarter = _get_const_value(config, "DRAWDOWN_QUARTER_THRESHOLD")
    halt = _get_const_value(config, "DRAWDOWN_HALT_THRESHOLD")
    if half is None and quarter is None and halt is None:
        return "*Drawdown thresholds not available*"
    lines = [
        "| Balance vs. Starting | Sizing Adjustment |",
        "|---|---|",
    ]
    if half is not None:
        lines.append(f"| \u2265 {half * 100:.0f}% | Full sizing |")
    if half is not None and quarter is not None:
        lines.append(f"| {quarter * 100:.0f}\u2013{half * 100:.0f}% | Half sizing |")
    if quarter is not None and halt is not None:
        lines.append(f"| {halt * 100:.0f}\u2013{quarter * 100:.0f}% | Quarter sizing |")
    if halt is not None:
        lines.append(f"| < {halt * 100:.0f}% | Halt trading |")
    return "\n".join(lines)


def _build_escalation_table(config):
    """Generate a markdown table from escalation wait times."""
    long_w = _get_const_value(config, "ESCALATION_WAIT_LONG")
    med_w = _get_const_value(config, "ESCALATION_WAIT_MEDIUM")
    short_w = _get_const_value(config, "ESCALATION_WAIT_SHORT")
    direct = _get_const_value(config, "DIRECT_TAKER_THRESHOLD")
    if long_w is None and med_w is None and short_w is None:
        return "*Escalation schedule not available*"
    lines = [
        "| STC Range | Maker Wait | Then |",
        "|:----------|:-----------|:-----|",
    ]
    if long_w is not None:
        lines.append(f"| \u2265 180s | {long_w:.0f}s | Escalate to taker |")
    if med_w is not None:
        lines.append(f"| 120--180s | {med_w:.0f}s | Escalate to taker |")
    if short_w is not None:
        lines.append(f"| 60--120s | {short_w:.0f}s | Escalate to taker |")
    if direct is not None:
        lines.append(f"| < {direct:.0f}s | 0s | Direct IOC taker |")
    return "\n".join(lines)


def _build_dynamic_cap_table(schedule, label="15M"):
    """Generate a markdown table from a DYNAMIC_CAP_SCHEDULE list of (stc, cap) tuples."""
    if not schedule:
        return f"*{label} dynamic cap schedule not available*"
    lines = [
        f"| STC Range ({label}) | Prob Cap |",
        "|:----------|:--------|",
    ]
    for i, (stc, cap) in enumerate(schedule):
        if i == 0:
            range_str = f"> {stc}s"
        else:
            prev_stc = schedule[i - 1][0]
            range_str = f"{stc}--{prev_stc}s"
        lines.append(f"| {range_str} | {cap * 100:.1f}% |")
    return "\n".join(lines)


def build_replacements(stats, config=None):
    """Build a flat dict of placeholder -> value from stats JSON and config JSON."""
    total = stats.get("total_evaluated", 0)
    fb = stats.get("filter_breakdown", {})
    wrp = stats.get("win_rate_by_price", {})
    total_settled = stats.get("total_settled", 0)
    total_wins = stats.get("total_wins", 0)
    total_trades = stats.get("total_trades", 0)

    # Map filter stage keys (handle both snake_case and display names)
    filter_map = {
        "low_prob": ["low_probability", "low_prob"],
        "no_ob": ["no_orderbook", "no_ob"],
        "no_ask": ["no_best_ask", "no_ask"],
        "price_oor": ["price_out_of_range", "price_oor"],
        "insuff_edge": ["insufficient_edge", "insuff_edge"],
        "zero_size": ["zero_sizing", "zero_size"],
        "strategy_wait": ["strategy_wait"],
        "candidate": ["candidate"],
    }

    def get_filter_count(keys):
        for k in keys:
            if k in fb:
                return fb[k]
        return 0

    f_low = get_filter_count(filter_map["low_prob"])
    f_no_ob = get_filter_count(filter_map["no_ob"])
    f_no_ask = get_filter_count(filter_map["no_ask"])
    f_price = get_filter_count(filter_map["price_oor"])
    f_edge = get_filter_count(filter_map["insuff_edge"])
    f_zero = get_filter_count(filter_map["zero_size"])
    f_wait = get_filter_count(filter_map["strategy_wait"])
    f_cand = get_filter_count(filter_map["candidate"])

    assets = stats.get("assets_tracked", ["BTC", "ETH", "SOL", "XRP"])

    # Find top rejection reason (exclude candidate)
    rejection_counts = {k: v for k, v in fb.items() if k != "candidate"}
    if rejection_counts:
        top_key = max(rejection_counts, key=rejection_counts.get)
        top_val = rejection_counts[top_key]
        top_rejection = f"{top_key.replace('_', ' ').title()} ({top_val:,})"
    else:
        top_rejection = "N/A"

    total_losses = total_settled - total_wins

    r = {
        "TOTAL_EVALUATED": f"{total:,}",
        "TOP_REJECTION": top_rejection,
        "TOTAL_SETTLED": f"{total_settled:,}",
        "TOTAL_WINS": f"{total_wins:,}",
        "TOTAL_LOSSES": f"{total_losses:,}",
        "OBSERVATION_PERIOD": stats.get("observation_period", "N/A"),
        "ASSETS_TRACKED": ", ".join(assets),
        "FILTER_LOW_PROB": f"{f_low:,}",
        "FILTER_LOW_PROB_PCT": pct(f_low, total),
        "FILTER_NO_OB": f"{f_no_ob:,}",
        "FILTER_NO_OB_PCT": pct(f_no_ob, total),
        "FILTER_NO_ASK": f"{f_no_ask:,}",
        "FILTER_NO_ASK_PCT": pct(f_no_ask, total),
        "FILTER_PRICE_OOR": f"{f_price:,}",
        "FILTER_PRICE_OOR_PCT": pct(f_price, total),
        "FILTER_INSUFF_EDGE": f"{f_edge:,}",
        "FILTER_INSUFF_EDGE_PCT": pct(f_edge, total),
        "FILTER_ZERO_SIZE": f"{f_zero:,}",
        "FILTER_ZERO_SIZE_PCT": pct(f_zero, total),
        "FILTER_STRATEGY_WAIT": f"{f_wait:,}",
        "FILTER_STRATEGY_WAIT_PCT": pct(f_wait, total),
        "FILTER_CANDIDATE": f"{f_cand:,}",
        "FILTER_CANDIDATE_PCT": pct(f_cand, total),
        "TOTAL_TRADES": f"{total_trades:,}",
        "OBSERVATION_PNL": f"{stats.get('observation_pnl', 0):,}",
        "WIN_RATE": rate(total_wins, total_settled) if total_settled > 0 else "N/A",
        "GENERATED_AT": stats.get("generated_at", "N/A"),
    }

    # Win rate by price bucket
    for bucket_key, prefix in [("80-84", "WR_80"), ("85-89", "WR_85"), ("90-94", "WR_90"), ("95-99", "WR_95")]:
        bucket = wrp.get(bucket_key, {"n": 0, "wins": 0})
        n = bucket.get("n", 0)
        w = bucket.get("wins", 0)
        r[f"{prefix}_N"] = str(n)
        r[f"{prefix}_W"] = str(w)
        r[f"{prefix}_R"] = rate(w, n)

    # ── Config-derived replacements ──────────────────────────────────────────
    if config:
        # Simple value replacements
        simple_replacements = {
            "MIN_ENTRY_PRICE": "MIN_ENTRY_PRICE",
            "MAX_ENTRY_PRICE": "MAX_ENTRY_PRICE",
            "DIRECT_TAKER_THRESHOLD": "DIRECT_TAKER_THRESHOLD",
            "MAX_RISK_PER_TRADE": "MAX_RISK_PER_TRADE",
            "MARKET_BLEND_W": "MARKET_BLEND_W",
            "MIN_EDGE_PCT": "MIN_EDGE_PCT",
            "MAX_SECONDS_BEFORE_CLOSE": "MAX_SECONDS_BEFORE_CLOSE",
            "STC_SHADOW_THRESHOLD": "STC_SHADOW_THRESHOLD",
            "PRICE_BUFFER_SIZE": "PRICE_BUFFER_SIZE",
            "Z_SCORE_MAX": "Z_SCORE_MAX",
            "MAKER_ONLY_THRESHOLD": "MAKER_ONLY_THRESHOLD",
            "DRAWDOWN_HALF_THRESHOLD": "DRAWDOWN_HALF_THRESHOLD",
            "DRAWDOWN_QUARTER_THRESHOLD": "DRAWDOWN_QUARTER_THRESHOLD",
            "DRAWDOWN_HALT_THRESHOLD": "DRAWDOWN_HALT_THRESHOLD",
            "ESCALATION_WAIT_LONG": "ESCALATION_WAIT_LONG",
            "ESCALATION_WAIT_MEDIUM": "ESCALATION_WAIT_MEDIUM",
            "ESCALATION_WAIT_SHORT": "ESCALATION_WAIT_SHORT",
            "HOURLY_TEMPERATURE_T": "HOURLY_TEMPERATURE_T",
            "HOURLY_KELLY_FRACTION": "HOURLY_KELLY_FRACTION",
            "HOURLY_MIN_STC_ENTRY": "HOURLY_MIN_STC_ENTRY",
            "HOURLY_MAX_STC_ENTRY": "HOURLY_MAX_STC_ENTRY",
            "HOURLY_MAX_POSITIONS_PER_WINDOW": "HOURLY_MAX_POSITIONS_PER_WINDOW",
            "HOURLY_MAX_WINDOW_RISK": "HOURLY_MAX_WINDOW_RISK",
            "HOURLY_MARKET_BLEND_W": "HOURLY_MARKET_BLEND_W",
            "HOURLY_MIN_ENTRY_PRICE": "HOURLY_MIN_ENTRY_PRICE",
            "HOURLY_MAX_RISK_PER_TRADE": "HOURLY_MAX_RISK_PER_TRADE",
            "CALIBRATION_MIN_SAMPLES_PLATT": "CALIBRATION_MIN_SAMPLES_PLATT",
            "CALIBRATION_MIN_SAMPLES_BETA": "CALIBRATION_MIN_SAMPLES_BETA",
            "CALIBRATION_MIN_SAMPLES_BLR": "CALIBRATION_MIN_SAMPLES_BLR",
        }
        for placeholder, const_key in simple_replacements.items():
            val = _get_const_value(config, const_key)
            if val is not None:
                # Format nicely: floats as-is, ints as-is
                if isinstance(val, float):
                    # Remove trailing zeros for cleaner display
                    r[placeholder] = f"{val:g}"
                else:
                    r[placeholder] = str(val)

        # Derived values
        blend_w = _get_const_value(config, "MARKET_BLEND_W")
        if blend_w is not None:
            r["MODEL_WEIGHT_PCT"] = f"{(1.0 - blend_w) * 100:.0f}"
            r["MARKET_WEIGHT_PCT"] = f"{blend_w * 100:.0f}"

        dd_half = _get_const_value(config, "DRAWDOWN_HALF_THRESHOLD")
        if dd_half is not None:
            r["DRAWDOWN_HALF_PCT"] = f"{dd_half * 100:.0f}"
        dd_quarter = _get_const_value(config, "DRAWDOWN_QUARTER_THRESHOLD")
        if dd_quarter is not None:
            r["DRAWDOWN_QUARTER_PCT"] = f"{dd_quarter * 100:.0f}"
        dd_halt = _get_const_value(config, "DRAWDOWN_HALT_THRESHOLD")
        if dd_halt is not None:
            r["DRAWDOWN_HALT_PCT"] = f"{dd_halt * 100:.0f}"

        # Bot line count
        bot_lines = config.get("_bot_lines")
        if bot_lines is not None:
            r["BOT_LINE_COUNT"] = f"{bot_lines:,}"

        # Table generators
        sizing_tiers = _get_const_value(config, "SIZING_TIERS")
        r["SIZING_TIERS_TABLE"] = _build_sizing_tiers_table(sizing_tiers)

        edge_schedule = _get_const_value(config, "MIN_EDGE_BY_PRICE")
        r["EDGE_SCHEDULE_TABLE"] = _build_edge_schedule_table(edge_schedule)

        r["DRAWDOWN_TABLE"] = _build_drawdown_table(config)
        r["ESCALATION_TABLE"] = _build_escalation_table(config)

        dyn_cap = _get_const_value(config, "DYNAMIC_CAP_SCHEDULE")
        r["DYNAMIC_CAP_TABLE"] = _build_dynamic_cap_table(dyn_cap, "15M")

        hourly_dyn_cap = _get_const_value(config, "HOURLY_DYNAMIC_CAP_SCHEDULE")
        r["HOURLY_DYNAMIC_CAP_TABLE"] = _build_dynamic_cap_table(hourly_dyn_cap, "Hourly")

        # Exchange feed list
        exchange_feeds = config.get("_exchange_feeds", [])
        if exchange_feeds:
            r["EXCHANGE_FEED_LIST"] = ", ".join(exchange_feeds)

        exchange_names = config.get("_exchange_names", [])
        if exchange_names:
            r["EXCHANGE_NAMES"] = ", ".join(exchange_names)

        # Cross-file data
        weather = config.get("_weather", {})
        if weather:
            r["WEATHER_CITY_COUNT"] = str(weather.get("weather_city_count", 0))
            r["WEATHER_CITY_NAMES"] = ", ".join(weather.get("weather_city_names", []))
            r["WEATHER_CITY_CODES"] = ", ".join(weather.get("weather_city_codes", []))

        sports = config.get("_sports", {})
        if sports:
            r["SPORTS_LEAGUE_COUNT"] = str(sports.get("sports_league_count", 0))
            r["SPORTS_LEAGUE_NAMES"] = ", ".join(sports.get("sports_league_names", []))

    return r


def main():
    if not os.path.exists(TEMPLATE_PATH):
        print(f"Error: {TEMPLATE_PATH} not found", file=sys.stderr)
        sys.exit(1)

    # Load stats (from VPS)
    if not os.path.exists(STATS_PATH):
        print(f"Warning: {STATS_PATH} not found, using empty stats", file=sys.stderr)
        stats = {}
    else:
        with open(STATS_PATH) as f:
            stats = json.load(f)
        if "error" in stats:
            print(f"Warning: stats JSON has error: {stats['error']}", file=sys.stderr)
            stats = {}

    # Load config (from extract_config.py)
    config = None
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            config = json.load(f)
        print(f"Loaded config.json ({len(config.get('constants', {}))} constants)", file=sys.stderr)
    else:
        print(f"Warning: {CONFIG_PATH} not found, config-derived placeholders will not be replaced", file=sys.stderr)

    with open(TEMPLATE_PATH) as f:
        template = f.read()

    replacements = build_replacements(stats, config)

    def replace_placeholder(match):
        key = match.group(1)
        return replacements.get(key, match.group(0))

    rendered = re.sub(r"\{\{(\w+)\}\}", replace_placeholder, template)

    with open(OUTPUT_PATH, "w") as f:
        f.write(rendered)

    print(f"Rendered whitepaper written to {OUTPUT_PATH}")
    unreplaced = re.findall(r"\{\{(\w+)\}\}", rendered)
    if unreplaced:
        print(f"Warning: {len(unreplaced)} unreplaced placeholders in whitepaper: {unreplaced}", file=sys.stderr)

    # Render investor whitepaper
    if os.path.exists(INVESTOR_TEMPLATE_PATH):
        with open(INVESTOR_TEMPLATE_PATH) as f:
            investor_template = f.read()

        investor_rendered = re.sub(r"\{\{(\w+)\}\}", replace_placeholder, investor_template)

        with open(INVESTOR_OUTPUT_PATH, "w") as f:
            f.write(investor_rendered)

        print(f"Rendered investor whitepaper written to {INVESTOR_OUTPUT_PATH}")
        unreplaced_investor = re.findall(r"\{\{(\w+)\}\}", investor_rendered)
        if unreplaced_investor:
            print(f"Warning: {len(unreplaced_investor)} unreplaced placeholders in investor whitepaper: {unreplaced_investor}", file=sys.stderr)

    # Render README
    if os.path.exists(README_TEMPLATE_PATH):
        with open(README_TEMPLATE_PATH) as f:
            readme_template = f.read()

        readme_rendered = re.sub(r"\{\{(\w+)\}\}", replace_placeholder, readme_template)

        with open(README_OUTPUT_PATH, "w") as f:
            f.write(readme_rendered)

        print(f"Rendered README written to {README_OUTPUT_PATH}")
        unreplaced_readme = re.findall(r"\{\{(\w+)\}\}", readme_rendered)
        if unreplaced_readme:
            print(f"Warning: {len(unreplaced_readme)} unreplaced placeholders in README: {unreplaced_readme}", file=sys.stderr)


if __name__ == "__main__":
    main()
