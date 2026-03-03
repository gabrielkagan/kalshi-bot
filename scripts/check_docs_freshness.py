#!/usr/bin/env python3
"""Check rendered docs for stale config values.

Compares key numbers in rendered docs against config.json extracted from bot.py
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
REPO_DIR = os.path.join(SCRIPT_DIR, "..")
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


def check_unreplaced_placeholders(path):
    """Find any remaining {{PLACEHOLDER}} markers in rendered docs."""
    if not os.path.exists(path):
        return []
    with open(path) as f:
        content = f.read()
    return re.findall(r"\{\{(\w+)\}\}", content)


def count_leagues():
    """Count leagues in sports_data.py LEAGUES dict."""
    sports_data_path = os.path.join(REPO_DIR, "sports_data.py")
    if not os.path.exists(sports_data_path):
        return None
    with open(sports_data_path) as f:
        content = f.read()
    return len(re.findall(r'"KX\w+":\s*LeagueConfig', content))


def count_weather_cities():
    """Count cities in weather_engine.py WEATHER_CITIES dict."""
    weather_path = os.path.join(REPO_DIR, "weather_engine.py")
    if not os.path.exists(weather_path):
        return None
    with open(weather_path) as f:
        content = f.read()
    return len(re.findall(r'"[A-Z]{3}":\s*\{', content))


def get_sports_config():
    """Extract key sports config values from sports_data.py."""
    sports_data_path = os.path.join(REPO_DIR, "sports_data.py")
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


def check_config_values(path, config):
    """Check that key config values in docs match bot.py config."""
    if not os.path.exists(path) or not config:
        return []
    with open(path) as f:
        content = f.read()

    issues = []
    constants = config.get("constants", {})

    # --- Drawdown thresholds ---
    dd_half = constants.get("DRAWDOWN_HALF_THRESHOLD", {}).get("value")
    dd_quarter = constants.get("DRAWDOWN_QUARTER_THRESHOLD", {}).get("value")

    if dd_half is not None:
        expected_pct = int(dd_half * 100)
        stale_vals = [90, 92] if expected_pct != 90 else []
        for stale in stale_vals:
            if re.search(rf"{stale}%.{{0,30}}(?:halve|half|Half)", content):
                issues.append(
                    f"Stale DRAWDOWN_HALF: found {stale}% near 'half' in doc, "
                    f"bot.py has {expected_pct}%"
                )

    if dd_quarter is not None:
        expected_pct = int(dd_quarter * 100)
        stale_vals = [80] if expected_pct != 80 else []
        for stale in stale_vals:
            if re.search(rf"{stale}%.{{0,10}}(?:quarter|one-quarter)", content):
                issues.append(
                    f"Stale DRAWDOWN_QUARTER: found {stale}% near 'quarter' in doc, "
                    f"bot.py has {expected_pct}%"
                )

    # --- Market blend (core crypto, not weather/SPX) ---
    blend_w = constants.get("MARKET_BLEND_W", {}).get("value")
    if blend_w is not None:
        model_pct = int((1.0 - blend_w) * 100)
        if model_pct != 50:
            blend_pattern = r"(?:model|calibrat|p_final|p_{final}).{0,60}(?:50[/%]50|0\.50\s*\\times.*0\.50)"
            if re.search(blend_pattern, content, re.IGNORECASE):
                issues.append(
                    f"Stale MARKET_BLEND: found 50/50 in doc, "
                    f"bot.py has {model_pct}/{int(blend_w * 100)}"
                )

    # --- Sizing tiers ---
    sizing = constants.get("SIZING_TIERS", {}).get("value")
    if sizing:
        old_edges = {"2%": 0.02, "1.5%": 0.015, "1%": 0.01}
        actual_edges = {t[0] for t in sizing}
        for label, val in old_edges.items():
            if val not in actual_edges and re.search(rf"≥\s*{label}\s*\|", content):
                issues.append(f"Stale SIZING_TIER: found ≥ {label} in doc, not in current tiers")

        # Check tier count — match "≥ X%" in sizing table rows (edge values ≤ 5%)
        actual_count = len(sizing)
        tier_rows = re.findall(r"≥\s*([\d.]+)%\s*\|", content)
        # Filter to edge-sized values (≤5%) to exclude drawdown thresholds (65-85%)
        edge_tiers = [t for t in tier_rows if float(t) <= 5.0]
        if edge_tiers and len(edge_tiers) != actual_count:
            issues.append(
                f"Stale SIZING_TIER count: doc has {len(edge_tiers)} tiers, "
                f"bot.py has {actual_count}"
            )

    # --- MIN_ENTRY_PRICE ---
    min_price = constants.get("MIN_ENTRY_PRICE", {}).get("value")
    if min_price is not None:
        # Check for wrong price in "XX–99¢" or "XX–99 cents" patterns
        # Require ¢ or "cent" nearby to avoid matching probability caps like "93–99.5%"
        price_pattern = r'(\d{2})[\–\-–—]99(?:¢|\s*cent)'
        price_mentions = re.findall(price_pattern, content)
        for found_price in price_mentions:
            if int(found_price) != min_price and int(found_price) in range(80, 99):
                issues.append(
                    f"Stale MIN_ENTRY_PRICE: found {found_price}–99¢ in doc, "
                    f"bot.py has {min_price}"
                )

    # --- MAKER_ONLY_THRESHOLD ---
    mot = constants.get("MAKER_ONLY_THRESHOLD", {}).get("value")
    if mot is not None and mot == 0.0:
        # Maker-only threshold is disabled — flag any doc claiming "no taker below Xs"
        if re.search(r'(?:maker.only|no taker).{0,30}(?:below|under)\s+\d+\s*(?:s|sec)', content, re.IGNORECASE):
            issues.append(
                "Stale MAKER_ONLY_THRESHOLD: doc claims maker-only zone, "
                "but MAKER_ONLY_THRESHOLD=0.0 (taker allowed everywhere)"
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
        # Find "XX leagues" mentions
        league_mentions = re.findall(r'(\d+)\s+leagues', content)
        for mention in league_mentions:
            if int(mention) != actual_leagues:
                issues.append(
                    f"Stale league count: found '{mention} leagues' in doc, "
                    f"sports_data.py has {actual_leagues}"
                )

    # --- Weather city count ---
    actual_cities = count_weather_cities()
    if actual_cities:
        # Find "X US cities" or "X cities" or "X major US cities"
        city_mentions = re.findall(r'(\d+)\s+(?:US\s+|major\s+US\s+)?cit(?:y|ies)', content)
        for mention in city_mentions:
            if int(mention) != actual_cities:
                issues.append(
                    f"Stale city count: found '{mention} cities' in doc, "
                    f"weather_engine.py has {actual_cities}"
                )

    # --- Sports LR scale ---
    sports_cfg = get_sports_config()
    lr_scale = sports_cfg.get("CONSERVATIVE_LR_SCALE")
    if lr_scale is not None:
        lr_pct = int(lr_scale * 100)
        # Look for "compressed XX%" patterns
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
        # Look for "more than XXpp" or "XX percentage points" patterns near model/market
        gap_mentions = re.findall(r'(?:more than|exceeds|>)\s+(\d+)\s*(?:pp|percentage point)', content, re.IGNORECASE)
        for mention in gap_mentions:
            if int(mention) != gap_pp:
                issues.append(
                    f"Stale MAX_MODEL_MARKET_GAP: found '{mention}pp' in doc, "
                    f"sports_data.py has {gap_pp}pp"
                )

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
