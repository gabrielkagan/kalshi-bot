#!/usr/bin/env python3
"""Render whitepaper.md and README.template.md by replacing {{PLACEHOLDER}} markers with live stats."""

import json
import os
import re
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.join(SCRIPT_DIR, "..")
TEMPLATE_PATH = os.path.join(REPO_DIR, "whitepaper.md")
STATS_PATH = os.path.join(REPO_DIR, "whitepaper_stats.json")
OUTPUT_PATH = os.path.join(REPO_DIR, "whitepaper_rendered.md")
INVESTOR_TEMPLATE_PATH = os.path.join(REPO_DIR, "whitepaper_investor.md")
INVESTOR_OUTPUT_PATH = os.path.join(REPO_DIR, "whitepaper_investor_rendered.md")
README_TEMPLATE_PATH = os.path.join(REPO_DIR, "README.template.md")
README_OUTPUT_PATH = os.path.join(REPO_DIR, "README.md")


def pct(n, total):
    """Format a percentage string."""
    if total == 0:
        return "0%"
    return f"{n / total * 100:.1f}%"


def rate(wins, n):
    """Format a win rate string."""
    if n == 0:
        return "—"
    return f"{wins / n * 100:.1f}%"


def build_replacements(stats):
    """Build a flat dict of placeholder -> value from stats JSON."""
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

    return r


def main():
    if not os.path.exists(TEMPLATE_PATH):
        print(f"Error: {TEMPLATE_PATH} not found", file=sys.stderr)
        sys.exit(1)

    if not os.path.exists(STATS_PATH):
        print(f"Warning: {STATS_PATH} not found, using empty stats", file=sys.stderr)
        stats = {}
    else:
        with open(STATS_PATH) as f:
            stats = json.load(f)

    with open(TEMPLATE_PATH) as f:
        template = f.read()

    replacements = build_replacements(stats)

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
