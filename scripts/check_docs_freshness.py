#!/usr/bin/env python3
"""Check rendered docs for stale config values.

Compares key numbers in rendered docs against config.json extracted from bot.py.
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


def check_config_values(path, config):
    """Check that key config values in docs match bot.py config."""
    if not os.path.exists(path) or not config:
        return []
    with open(path) as f:
        content = f.read()

    issues = []
    constants = config.get("constants", {})

    # Check drawdown thresholds
    dd_half = constants.get("DRAWDOWN_HALF_THRESHOLD", {}).get("value")
    dd_quarter = constants.get("DRAWDOWN_QUARTER_THRESHOLD", {}).get("value")
    dd_halt = constants.get("DRAWDOWN_HALT_THRESHOLD", {}).get("value")

    if dd_half is not None:
        expected_pct = int(dd_half * 100)
        # Look for stale patterns: "At 90% ... halve/half"
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
            # Require stale value to be immediately before "quarter" (not separated by other thresholds)
            if re.search(rf"{stale}%.{{0,10}}(?:quarter|one-quarter)", content):
                issues.append(
                    f"Stale DRAWDOWN_QUARTER: found {stale}% near 'quarter' in doc, "
                    f"bot.py has {expected_pct}%"
                )

    # Check market blend — only flag 50/50 when near core model/probability/blend
    # language, not in engine-specific config tables (weather uses 50/50 legitimately)
    blend_w = constants.get("MARKET_BLEND_W", {}).get("value")
    if blend_w is not None:
        model_pct = int((1.0 - blend_w) * 100)
        market_pct = int(blend_w * 100)
        if model_pct != 50:
            # Match 50/50 only near core blend context (model/market/calibrated/final)
            blend_pattern = r"(?:model|calibrat|p_final|p_{final}).{0,60}(?:50[/%]50|0\.50\s*\\times.*0\.50)"
            if re.search(blend_pattern, content, re.IGNORECASE):
                issues.append(
                    f"Stale MARKET_BLEND: found 50/50 in doc, "
                    f"bot.py has {model_pct}/{market_pct}"
                )

    # Check sizing tiers
    sizing = constants.get("SIZING_TIERS", {}).get("value")
    if sizing:
        # Check for old tier values (0.02, 0.015, 0.01 edge thresholds)
        old_edges = {"2%": 0.02, "1.5%": 0.015, "1%": 0.01}
        actual_edges = {t[0] for t in sizing}
        for label, val in old_edges.items():
            if val not in actual_edges and re.search(rf"≥\s*{label}\s*\|", content):
                issues.append(f"Stale SIZING_TIER: found ≥ {label} in doc, not in current tiers")

    return issues


def main():
    strict = "--strict" in sys.argv
    config = load_config()

    all_issues = []
    all_unreplaced = []

    for path in RENDERED_DOCS:
        name = os.path.basename(path)
        if not os.path.exists(path):
            continue

        # Check unreplaced placeholders
        unreplaced = check_unreplaced_placeholders(path)
        if unreplaced:
            all_unreplaced.append((name, unreplaced))

        # Check config values
        if config:
            issues = check_config_values(path, config)
            for issue in issues:
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
        found = [os.path.basename(p) for p in RENDERED_DOCS if os.path.exists(p)]
        print(f"  All docs fresh ({len(found)} checked)")

    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
