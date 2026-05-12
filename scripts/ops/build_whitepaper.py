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
# Bit 11.2 (2026-05-12): relocated to scripts/ops/; need 2 ".." levels.
REPO_DIR = os.path.join(SCRIPT_DIR, "..", "..")
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
        # Note: legacy {{OBSERVATION_PNL}} placeholder DROPPED — the LABEL was the lie.
        # Templates using it will render literal `{{OBSERVATION_PNL}}` and trigger the
        # check_docs_freshness.py warning, forcing migration to LIVE_PNL_DOLLARS.
        "WIN_RATE": rate(total_wins, total_settled) if total_settled > 0 else "N/A",
        "GENERATED_AT": stats.get("generated_at", "N/A"),
    }

    # ── Corrected stats placeholders (post-2026-04-26 generator patch) ──────
    # All settled_trades rows are LIVE money. Shadow / hypothetical PnL comes from
    # evaluated_opportunities.counterfactual_pnl, exposed via SHADOW_PNL_*_CENTS below.
    # See kb/decisions/doc-rewrite-2026-04-26.md.
    live_pnl = stats.get("live_pnl_cents", 0)
    live_n = stats.get("live_settled", 0)
    live_w = stats.get("live_wins", 0)
    live_l = stats.get("live_losses", 0)
    live_be = stats.get("live_breakevens", 0)
    live_pnl_h = stats.get("live_pnl_headline_cents", 0)
    live_n_h = stats.get("live_settled_headline", 0)

    r["LIVE_PNL_CENTS"] = f"{live_pnl:,}"
    r["LIVE_PNL_DOLLARS"] = f"{live_pnl / 100:,.2f}"
    r["LIVE_SETTLED"] = f"{live_n:,}"
    r["LIVE_WINS"] = f"{live_w:,}"
    r["LIVE_LOSSES"] = f"{live_l:,}"
    r["LIVE_BREAKEVENS"] = f"{live_be:,}"
    r["LIVE_WR"] = rate(live_w, live_n)
    # Headline = LIVE excluding 1ct verification (weather_no_live) + kill-switched (hourly_no_live)
    r["LIVE_PNL_HEADLINE_CENTS"] = f"{live_pnl_h:,}"
    r["LIVE_PNL_HEADLINE_DOLLARS"] = f"{live_pnl_h / 100:,.2f}"
    r["LIVE_SETTLED_HEADLINE"] = f"{live_n_h:,}"
    r["TOTAL_LIVE_CANDIDATES"] = f"{stats.get('total_live_candidates', 0):,}"

    # Shadow / observation PnL — from evaluated_opportunities.counterfactual_pnl,
    # NOT settled_trades (which has no observation rows).
    shadow_by_stage = stats.get("shadow_counterfactual_pnl_by_stage", {}) or {}
    shadow_total = sum(s.get("pnl_cents", 0) for s in shadow_by_stage.values())
    shadow_total_n = sum(s.get("n", 0) for s in shadow_by_stage.values())
    r["SHADOW_PNL_CENTS"] = f"{shadow_total:,}"
    r["SHADOW_PNL_DOLLARS"] = f"{shadow_total / 100:,.2f}"
    r["SHADOW_TOTAL_N"] = f"{shadow_total_n:,}"

    # Per-product 15M filter funnel (uses 15M-only denominator, not the global one
    # which was inflated by hourly/spx/weather/sports observation logs)
    fbp = stats.get("filter_breakdown_by_product", {})
    fb_15m = fbp.get("15m", {})
    fb_15m_total = sum(fb_15m.values()) if fb_15m else 0
    fb_15m_cands = fb_15m.get("candidate", 0)
    r["FIFTEENM_TOTAL_EVALUATED"] = f"{fb_15m_total:,}"
    r["FIFTEENM_CANDIDATES"] = f"{fb_15m_cands:,}"
    r["FIFTEENM_PASS_RATE"] = pct(fb_15m_cands, fb_15m_total) if fb_15m_total else "N/A"
    r["FIFTEENM_INSUFF_EDGE"] = f"{fb_15m.get('insufficient_edge', 0):,}"
    r["FIFTEENM_PRICE_OOR"] = f"{fb_15m.get('price_out_of_range', 0):,}"

    # Brier scores. Two flavors:
    #   BRIER_*           = all live-candidate evaluations (filled + unfilled). Measures the
    #                       MODEL'S calibration on viable opportunities.
    #   BRIER_FILLED_*    = filled trades only (JOIN settled_trades). Measures the BOT'S
    #                       paid-decision calibration. Use this for headline calibration claims.
    brier = stats.get("brier") or {}
    overall_brier = brier.get("overall")
    r["BRIER_OVERALL"] = f"{overall_brier:.4f}" if overall_brier is not None else "N/A"
    by_product_brier = brier.get("by_product") or {}
    for pt_key, pt_label in [
        ("15m", "BRIER_15M"),
        ("hourly", "BRIER_HOURLY"),
        ("weather", "BRIER_WEATHER"),
        ("spx_hourly", "BRIER_SPX"),
        ("sports", "BRIER_SPORTS"),
    ]:
        v = by_product_brier.get(pt_key)
        r[pt_label] = f"{v:.4f}" if v is not None else "N/A"

    brier_f = stats.get("brier_filled") or {}
    overall_bf = brier_f.get("overall")
    r["BRIER_FILLED_OVERALL"] = f"{overall_bf:.4f}" if overall_bf is not None else "N/A"
    r["BRIER_FILLED_N"] = f"{brier_f.get('n', 0):,}"
    bf_by_product = brier_f.get("by_product") or {}
    for pt_key, pt_label in [
        ("15m", "BRIER_FILLED_15M"),
        ("hourly", "BRIER_FILLED_HOURLY"),
        ("weather", "BRIER_FILLED_WEATHER"),
    ]:
        v = bf_by_product.get(pt_key)
        r[pt_label] = f"{v:.4f}" if v is not None else "N/A"

    # Per-strategy_group settled stats (for the deep whitepaper rewrite)
    sbsg = stats.get("settled_by_strategy_group", {}) or {}
    for sg_key, sg_prefix in [
        ("main", "SG_MAIN"),
        ("decided", "SG_DECIDED"),
        ("weekend_discount", "SG_WEEKEND"),
        ("overnight_discount", "SG_OVERNIGHT"),
        ("low_price_near_expiry", "SG_LPNE"),
        ("weather_no_live", "SG_WEATHER_NO"),
        ("hourly_no_live", "SG_HOURLY_NO"),
    ]:
        sg = sbsg.get(sg_key, {"n": 0, "wins": 0, "pnl_cents": 0})
        n = sg.get("n", 0)
        w = sg.get("wins", 0)
        p = sg.get("pnl_cents", 0)
        r[f"{sg_prefix}_N"] = f"{n:,}"
        r[f"{sg_prefix}_WINS"] = f"{w:,}"
        r[f"{sg_prefix}_LOSSES"] = f"{(n - w):,}"
        r[f"{sg_prefix}_PNL_CENTS"] = f"{p:,}"
        r[f"{sg_prefix}_PNL_DOLLARS"] = f"{p / 100:,.2f}"
        r[f"{sg_prefix}_WR"] = rate(w, n)

    # Regime-filtered blocks. Cutoffs pinned to actual deploy commits (UTC):
    #   APR11 = 2026-04-11T20:43:27Z (commit 4075655 — cooldown julianday + weather NO fix)
    #   APR23 = 2026-04-23T23:46:07Z (commit 0ddcaf8 — WS schema yes_dollars_fp)
    for regime_key, regime_prefix in [("since_apr11", "APR11"), ("since_apr23", "APR23")]:
        rb = stats.get(regime_key, {}) or {}
        r[f"{regime_prefix}_LIVE_PNL_CENTS"] = f"{rb.get('live_pnl_cents', 0):,}"
        r[f"{regime_prefix}_LIVE_PNL_DOLLARS"] = f"{rb.get('live_pnl_cents', 0) / 100:,.2f}"
        r[f"{regime_prefix}_LIVE_SETTLED"] = f"{rb.get('live_settled', 0):,}"
        r[f"{regime_prefix}_LIVE_WINS"] = f"{rb.get('live_wins', 0):,}"
        r[f"{regime_prefix}_LIVE_WR"] = rate(rb.get("live_wins", 0), rb.get("live_settled", 0))
        rb_brier = rb.get("brier_overall")
        r[f"{regime_prefix}_BRIER"] = f"{rb_brier:.4f}" if rb_brier is not None else "N/A"

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
        bot_lines = config.get("_combined_source_lines")
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
