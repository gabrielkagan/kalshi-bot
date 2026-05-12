#!/usr/bin/env python3
"""Check rendered docs for stale config values.

Compares key numbers in rendered docs against config.json extracted from bot/_impl.py
and against hardcoded cross-file checks (sports, weather, etc.).
Returns exit code 1 if any values are stale. Designed to run in CI or locally.

Usage:
    python3 scripts/check_docs_freshness.py
    python3 scripts/check_docs_freshness.py --strict  # fail on unreplaced placeholders too
"""

import json
import os
import re
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# Bit 11.2 (2026-05-12): relocated to scripts/audit/; need 2 ".." levels.
REPO_DIR = os.path.join(SCRIPT_DIR, "..", "..")
CONFIG_JSON = os.path.join(REPO_DIR, "config.json")

RENDERED_DOCS = [
    os.path.join(REPO_DIR, "README.md"),
    os.path.join(REPO_DIR, "whitepaper_rendered.md"),
    os.path.join(REPO_DIR, "whitepaper_investor_rendered.md"),
]

# Also check the template sources for hardcoded values
TEMPLATE_DOCS = [
    os.path.join(REPO_DIR, "whitepaper.md"),
    os.path.join(REPO_DIR, "whitepaper_investor.md"),
]


def load_config():
    """Load config.json if available."""
    if not os.path.exists(CONFIG_JSON):
        return None
    with open(CONFIG_JSON) as f:
        return json.load(f)


def _get_const(config, key, default=None):
    """Get constant value from config."""
    return config.get("constants", {}).get(key, {}).get("value", default)


def check_unreplaced_placeholders(path):
    """Find any remaining {{PLACEHOLDER}} markers in rendered docs."""
    if not os.path.exists(path):
        return []
    with open(path) as f:
        content = f.read()
    return re.findall(r"\{\{(\w+)\}\}", content)


def count_leagues():
    """Count leagues in bot/engines/sports_data.py LEAGUES dict."""
    sports_data_path = os.path.join(REPO_DIR, "bot", "engines", "sports_data.py")  # Sprint 10.1a (2026-05-11)
    if not os.path.exists(sports_data_path):
        return None
    with open(sports_data_path) as f:
        content = f.read()
    return len(re.findall(r'"KX\w+":\s*LeagueConfig', content))


def count_weather_cities():
    """Count cities in bot/engines/weather_engine.py WEATHER_CITIES dict."""
    weather_path = os.path.join(REPO_DIR, "bot", "engines", "weather_engine.py")  # Sprint 10.1c (2026-05-11)
    if not os.path.exists(weather_path):
        return None
    with open(weather_path) as f:
        content = f.read()
    return len(re.findall(r'"[A-Z]{2,4}":\s*\{', content))


def get_sports_config():
    """Extract key sports config values from bot/engines/sports_data.py."""
    sports_data_path = os.path.join(REPO_DIR, "bot", "engines", "sports_data.py")  # Sprint 10.1a (2026-05-11)
    if not os.path.exists(sports_data_path):
        return {}
    with open(sports_data_path) as f:
        content = f.read()

    config = {}
    m = re.search(r'CONSERVATIVE_LR_SCALE\s*=\s*([\d.]+)', content)
    if m:
        config["CONSERVATIVE_LR_SCALE"] = float(m.group(1))

    m = re.search(r'MAX_MODEL_MARKET_GAP\s*=\s*([\d.]+)', content)
    if m:
        config["MAX_MODEL_MARKET_GAP"] = float(m.group(1))

    return config


def get_exchange_feeds_from_bot():
    """Extract exchange feed class names directly from bot/_impl.py source."""
    bot_path = os.path.join(REPO_DIR, "bot/_impl.py")
    if not os.path.exists(bot_path):
        return []
    with open(bot_path) as f:
        content = f.read()
    return re.findall(r'class\s+(\w+(?:Feed|WebSocket))', content)


def check_config_values(path, config):
    """Check that key config values in docs match bot/_impl.py config."""
    if not os.path.exists(path) or not config:
        return []
    with open(path) as f:
        content = f.read()

    issues = []

    # --- Drawdown thresholds ---
    dd_half = _get_const(config, "DRAWDOWN_HALF_THRESHOLD")
    dd_quarter = _get_const(config, "DRAWDOWN_QUARTER_THRESHOLD")
    dd_halt = _get_const(config, "DRAWDOWN_HALT_THRESHOLD")

    # Check drawdown thresholds — match patterns where a percentage is directly
    # paired with a drawdown action (e.g. "85% | Half" or "below 85% ... halve")
    # Avoid matching prose like "quartered below 75%, halted below 65%" which
    # correctly states both thresholds in one sentence.
    for line in content.splitlines():
        if dd_half is not None:
            expected_pct = int(dd_half * 100)
            # Match "X% | Half" (table) or "below X% ... halve/half" (not also mentioning quarter/halt)
            dd_half_mentions = re.findall(r'(?:^|\|)\s*[^\|]*?(\d+)%\s*\|[^\|]*(?:halve|half|Half)', line)
            for mention in dd_half_mentions:
                if int(mention) != expected_pct and int(mention) in range(50, 100):
                    issues.append(
                        f"Stale DRAWDOWN_HALF: found {mention}% near 'half' in doc, "
                        f"bot/_impl.py has {expected_pct}%"
                    )

        if dd_quarter is not None:
            expected_pct = int(dd_quarter * 100)
            dd_quarter_mentions = re.findall(r'(?:^|\|)\s*[^\|]*?(\d+)%\s*\|[^\|]*[Qq]uarter', line)
            for mention in dd_quarter_mentions:
                if int(mention) != expected_pct and int(mention) in range(50, 100):
                    issues.append(
                        f"Stale DRAWDOWN_QUARTER: found {mention}% near 'quarter' in doc, "
                        f"bot/_impl.py has {expected_pct}%"
                    )

        if dd_halt is not None:
            expected_pct = int(dd_halt * 100)
            dd_halt_mentions = re.findall(r'(?:^|\|)\s*[^\|]*?(\d+)%\s*\|[^\|]*[Hh]alt', line)
            for mention in dd_halt_mentions:
                if int(mention) != expected_pct and int(mention) in range(50, 100):
                    issues.append(
                        f"Stale DRAWDOWN_HALT: found {mention}% near 'halt' in doc, "
                        f"bot/_impl.py has {expected_pct}%"
                    )

    # --- Market blend (core crypto, not weather/SPX) ---
    blend_w = _get_const(config, "MARKET_BLEND_W")
    if blend_w is not None:
        model_pct = int((1.0 - blend_w) * 100)
        if model_pct != 50:
            blend_pattern = r"(?:model|calibrat|p_final|p_{final}).{0,60}(?:50[/%]50|0\.50\s*\\times.*0\.50)"
            if re.search(blend_pattern, content, re.IGNORECASE):
                issues.append(
                    f"Stale MARKET_BLEND: found 50/50 in doc, "
                    f"bot/_impl.py has {model_pct}/{int(blend_w * 100)}"
                )

    # --- Sizing tiers ---
    sizing = _get_const(config, "SIZING_TIERS")
    if sizing:
        old_edges = {"2%": 0.02, "1.5%": 0.015, "1%": 0.01}
        actual_edges = {t[0] for t in sizing}
        for label, val in old_edges.items():
            if val not in actual_edges and re.search(rf"(?:>=|\u2265)\s*{label}\s*\|", content):
                issues.append(f"Stale SIZING_TIER: found {label} in doc, not in current tiers")

        # Check tier count
        actual_count = len(sizing)
        tier_rows = re.findall(r"(?:>=|\u2265)\s*([\d.]+)%\s*\|", content)
        edge_tiers = [t for t in tier_rows if float(t) <= 5.0]
        if edge_tiers and len(edge_tiers) != actual_count:
            issues.append(
                f"Stale SIZING_TIER count: doc has {len(edge_tiers)} tiers, "
                f"bot/_impl.py has {actual_count}"
            )

    # --- MIN_ENTRY_PRICE ---
    min_price = _get_const(config, "MIN_ENTRY_PRICE")
    if min_price is not None:
        price_pattern = r'(\d{2})[\-\u2013\u2014]99(?:\xA2|\s*cent)'
        price_mentions = re.findall(price_pattern, content)
        for found_price in price_mentions:
            if int(found_price) != min_price and int(found_price) in range(80, 99):
                issues.append(
                    f"Stale MIN_ENTRY_PRICE: found {found_price}--99c in doc, "
                    f"bot/_impl.py has {min_price}"
                )

    # --- MAKER_ONLY_THRESHOLD ---
    mot = _get_const(config, "MAKER_ONLY_THRESHOLD")
    if mot is not None and mot == 0.0:
        if re.search(r'(?:maker.only|no taker).{0,30}(?:below|under)\s+\d+\s*(?:s|sec)', content, re.IGNORECASE):
            issues.append(
                "Stale MAKER_ONLY_THRESHOLD: doc claims maker-only zone, "
                "but MAKER_ONLY_THRESHOLD=0.0 (taker allowed everywhere)"
            )

    # --- DIRECT_TAKER_THRESHOLD ---
    direct_taker = _get_const(config, "DIRECT_TAKER_THRESHOLD")
    if direct_taker is not None:
        # Look for "direct taker" or "IOC taker" near a number of seconds
        dt_mentions = re.findall(
            r'(?:direct\s+taker|IOC\s+taker|skip\s+maker).{0,40}(?:below|under|<)\s*(\d+)\s*(?:s|sec)',
            content, re.IGNORECASE
        )
        for mention in dt_mentions:
            if int(mention) != int(direct_taker):
                issues.append(
                    f"Stale DIRECT_TAKER_THRESHOLD: found '{mention}s' in doc, "
                    f"bot/_impl.py has {int(direct_taker)}s"
                )

    # --- Escalation wait times ---
    esc_long = _get_const(config, "ESCALATION_WAIT_LONG")
    if esc_long is not None:
        # Match patterns like "15s maker wait" or "wait 15s" near "escalat"
        esc_mentions = re.findall(
            r'(?:maker\s+wait|wait\s+time).{0,30}(?:>=?\s*180|long).{0,20}(\d+)\s*(?:s|sec)',
            content, re.IGNORECASE
        )
        for mention in esc_mentions:
            if int(mention) != int(esc_long):
                issues.append(
                    f"Stale ESCALATION_WAIT_LONG: found '{mention}s' in doc, "
                    f"bot/_impl.py has {int(esc_long)}s"
                )

    # --- Dynamic cap schedule values ---
    dyn_cap = _get_const(config, "DYNAMIC_CAP_SCHEDULE")
    if dyn_cap and isinstance(dyn_cap, list):
        # Check for stale cap values (e.g., "93%" cap mentioned but actual is different)
        for stc_threshold, cap_val in dyn_cap:
            cap_pct = f"{cap_val * 100:.1f}"
            # Look for lines mentioning this STC threshold with wrong cap
            if stc_threshold == dyn_cap[0][0]:  # first entry (highest STC)
                # e.g., "> 600s" or "> 10 min" with cap
                pattern = rf'>\s*{stc_threshold}s?.{{0,30}}(\d{{2,3}}(?:\.\d+)?)%'
                matches = re.findall(pattern, content)
                for m in matches:
                    if m != cap_pct and abs(float(m) - cap_val * 100) > 0.2:
                        issues.append(
                            f"Stale DYNAMIC_CAP at >{stc_threshold}s: found {m}% in doc, "
                            f"bot/_impl.py has {cap_pct}%"
                        )

    # --- CalibrationEngine sample thresholds ---
    for cal_key, label in [
        ("CALIBRATION_MIN_SAMPLES_PLATT", "Platt"),
        ("CALIBRATION_MIN_SAMPLES_BETA", "Beta"),
        ("CALIBRATION_MIN_SAMPLES_BLR", "BLR"),
    ]:
        cal_val = _get_const(config, cal_key)
        if cal_val is not None:
            # Look for "NNN samples" or "NNN observations" near the calibration method name
            cal_mentions = re.findall(
                rf'{label}.{{0,40}}(\d+)\s*(?:sample|observation|data\s*point)',
                content, re.IGNORECASE
            )
            for mention in cal_mentions:
                if int(mention) != int(cal_val):
                    issues.append(
                        f"Stale {cal_key}: found '{mention} samples' near {label} in doc, "
                        f"bot/_impl.py has {int(cal_val)}"
                    )

    return issues


def check_cross_file_values(path):
    """Check hardcoded cross-file values (sports leagues, weather cities, etc.)."""
    if not os.path.exists(path):
        return []
    with open(path) as f:
        content = f.read()

    issues = []

    # --- Sports league count ---
    actual_leagues = count_leagues()
    if actual_leagues:
        league_mentions = re.findall(r'(\d+)\s+leagues', content)
        for mention in league_mentions:
            if int(mention) != actual_leagues:
                issues.append(
                    f"Stale league count: found '{mention} leagues' in doc, "
                    f"bot/engines/sports_data.py has {actual_leagues}"
                )

    # --- Weather city count ---
    actual_cities = count_weather_cities()
    if actual_cities:
        city_mentions = re.findall(r'(\d+)\s+(?:US\s+|major\s+US\s+)?cit(?:y|ies)', content)
        for mention in city_mentions:
            if int(mention) != actual_cities:
                issues.append(
                    f"Stale city count: found '{mention} cities' in doc, "
                    f"bot/engines/weather_engine.py has {actual_cities}"
                )

    # --- Sports LR scale ---
    sports_cfg = get_sports_config()
    lr_scale = sports_cfg.get("CONSERVATIVE_LR_SCALE")
    if lr_scale is not None:
        compress_mentions = re.findall(r'compress(?:ed|ion)\s+(\d+)%', content, re.IGNORECASE)
        for mention in compress_mentions:
            actual_compress = int((1.0 - lr_scale) * 100)
            if int(mention) != actual_compress:
                issues.append(
                    f"Stale LR_SCALE: found 'compressed {mention}%' in doc, "
                    f"CONSERVATIVE_LR_SCALE={lr_scale} means {actual_compress}% compression"
                )

    # --- Sports model-market gap ---
    gap = sports_cfg.get("MAX_MODEL_MARKET_GAP")
    if gap is not None:
        gap_pp = int(gap * 100)
        gap_mentions = re.findall(r'(?:more than|exceeds|>)\s+(\d+)\s*(?:pp|percentage point)', content, re.IGNORECASE)
        for mention in gap_mentions:
            if int(mention) != gap_pp:
                issues.append(
                    f"Stale MAX_MODEL_MARKET_GAP: found '{mention}pp' in doc, "
                    f"bot/engines/sports_data.py has {gap_pp}pp"
                )

    # --- Exchange feed list ---
    actual_feeds = get_exchange_feeds_from_bot()
    if actual_feeds:
        # Check for exchange names that appear in docs but not in actual feed classes
        # Map common exchange names to their feed class patterns
        exchange_check = {
            "Coinbase": "CoinbaseFeed",
            "Binance": "CrossExchangeFeed",  # Binance is inside CrossExchangeFeed
            "Kraken": "CrossExchangeFeed",
            "Bybit": "CrossExchangeFeed",
        }
        # If "CrossExchangeFeed" is not in actual_feeds, flag mentions of those exchanges
        has_cross = any("CrossExchange" in f for f in actual_feeds)
        has_coinbase = any("Coinbase" in f for f in actual_feeds)

        if not has_coinbase:
            if re.search(r'\bCoinbase\b', content):
                issues.append("Doc mentions Coinbase but no CoinbaseFeed class found in bot/_impl.py")
        if not has_cross:
            for ex in ["Binance", "Kraken", "Bybit"]:
                if re.search(rf'\b{ex}\b', content, re.IGNORECASE):
                    issues.append(f"Doc mentions {ex} but no CrossExchangeFeed class found in bot/_impl.py")

    return issues


def main():
    strict = "--strict" in sys.argv
    config = load_config()

    all_issues = []
    all_unreplaced = []

    # Check rendered docs (with template vars replaced)
    for path in RENDERED_DOCS:
        name = os.path.basename(path)
        if not os.path.exists(path):
            continue

        unreplaced = check_unreplaced_placeholders(path)
        if unreplaced:
            all_unreplaced.append((name, unreplaced))

        if config:
            issues = check_config_values(path, config)
            for issue in issues:
                all_issues.append((name, issue))

        cross_issues = check_cross_file_values(path)
        for issue in cross_issues:
            all_issues.append((name, issue))

    # Also check template sources for hardcoded values
    for path in TEMPLATE_DOCS:
        name = os.path.basename(path)
        if not os.path.exists(path):
            continue

        if config:
            issues = check_config_values(path, config)
            for issue in issues:
                all_issues.append((name, issue))

        cross_issues = check_cross_file_values(path)
        for issue in cross_issues:
            all_issues.append((name, issue))

    # Report
    ok = True
    if all_unreplaced:
        for name, placeholders in all_unreplaced:
            print(f"  WARN [{name}]: {len(placeholders)} unreplaced placeholder(s): {placeholders}")
        if strict:
            ok = False

    if all_issues:
        for name, issue in all_issues:
            print(f"  STALE [{name}]: {issue}")
        ok = False

    if ok:
        found_rendered = [os.path.basename(p) for p in RENDERED_DOCS if os.path.exists(p)]
        found_templates = [os.path.basename(p) for p in TEMPLATE_DOCS if os.path.exists(p)]
        total = len(found_rendered) + len(found_templates)
        print(f"  All docs fresh ({total} checked)")

    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
