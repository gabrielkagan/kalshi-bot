#!/usr/bin/env python3
"""
Shadow Strategy Evaluator — evaluates all shadow strategies and recommends
promotion decisions based on win rate, PnL, and statistical confidence.

Usage:
    python3 scripts/audit/shadow_eval.py --db state.db --days 14
"""

import argparse
import math
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


# ── Known shadow filter_stages ──────────────────────────────────────────────
KNOWN_SHADOW_STAGES = {
    "decided_contract_t1",
    "decided_contract_t2",
    "relaxed_edge_shadow",
    "weekend_discount_shadow",
    "overnight_discount_shadow",
    "overnight_lp_shadow",
    "stc_shadow",
    "stc_shadow_no_xrp",
    "stc_shadow_xrp",
    "xrp_shadow",
    "price_shadow",
    "price_shadow_no_xrp",
    "price_shadow_xrp",
}

# filter_stages that are NOT shadow (exclude from discovery)
NON_SHADOW_STAGES = {
    "candidate",
    "insufficient_edge",
    "observation_trade",
    "hourly_observation",
    "spx_hourly_observation",
    "weather_observation",
}


def wilson_lower(wins: int, n: int, z: float = 1.96) -> float:
    """Wilson score 95% CI lower bound."""
    if n == 0:
        return 0.0
    w = wins / n
    denominator = 1 + z * z / n
    centre = w + z * z / (2 * n)
    spread = z * math.sqrt((w * (1 - w) + z * z / (4 * n)) / n)
    return (centre - spread) / denominator


def format_dollars(cents: float) -> str:
    """Format cents as dollars with sign."""
    dollars = cents / 100
    if dollars >= 0:
        return f"+${dollars:,.2f}"
    return f"-${abs(dollars):,.2f}"


def evaluate_strategy(rows: list[sqlite3.Row]) -> dict:
    """Evaluate a single shadow strategy from its DB rows."""
    total = len(rows)

    settled = [r for r in rows if r["status"] == "settled"]
    pending = [r for r in rows if r["status"] == "pending"]

    settled_count = len(settled)
    pending_count = len(pending)

    # Win/loss (YES side wins: market_result in yes/all_yes)
    wins = sum(1 for r in settled if r["market_result"] in ("yes", "all_yes"))
    losses = settled_count - wins

    wr = (wins / settled_count * 100) if settled_count > 0 else 0.0

    # Average price, edge, STC across all signals
    prices = [r["market_price"] for r in rows if r["market_price"] is not None]
    edges = [r["edge"] for r in rows if r["edge"] is not None]
    stcs = [r["seconds_to_close"] for r in rows if r["seconds_to_close"] is not None]

    avg_price = sum(prices) / len(prices) if prices else 0.0
    avg_edge = sum(edges) / len(edges) if edges else 0.0
    avg_stc = sum(stcs) / len(stcs) if stcs else 0.0

    # Counterfactual PnL
    pnl_values = [r["counterfactual_pnl"] for r in settled
                  if r["counterfactual_pnl"] is not None]

    if pnl_values:
        total_pnl_cents = sum(pnl_values)
    elif settled_count > 0:
        # Manual computation: wins × (100 - avg_price) - losses × avg_price, per contract
        # Scale by average position_size
        pos_sizes = [r["position_size"] for r in settled
                     if r["position_size"] is not None]
        avg_pos = sum(pos_sizes) / len(pos_sizes) if pos_sizes else 1.0
        total_pnl_cents = (
            wins * (100 - avg_price) * avg_pos
            - losses * avg_price * avg_pos
        )
    else:
        total_pnl_cents = 0.0

    # Breakeven analysis
    breakeven_wr = avg_price / 100 if avg_price > 0 else 0.5
    margin = (wr / 100) - breakeven_wr

    # Wilson score
    w_lower = wilson_lower(wins, settled_count) if settled_count > 0 else 0.0

    # Promotion decision
    if settled_count >= 50:
        if (wr / 100 > breakeven_wr + 0.02
                and total_pnl_cents > 0
                and w_lower > breakeven_wr):
            decision = "PROMOTE"
            reasons = []
            reasons.append(f"WR {wr:.1f}% > BE+2pp ({breakeven_wr*100:.1f}%+2)")
            reasons.append(f"PnL {format_dollars(total_pnl_cents)}")
            reasons.append(f"Wilson lower {w_lower*100:.1f}% > BE {breakeven_wr*100:.1f}%")
            reasons.append(f"n={settled_count} >= 50")
        elif wr / 100 < breakeven_wr and total_pnl_cents < 0:
            decision = "KILL"
            reasons = []
            reasons.append(f"WR {wr:.1f}% < BE {breakeven_wr*100:.1f}%")
            reasons.append(f"PnL {format_dollars(total_pnl_cents)}")
            reasons.append(f"n={settled_count}")
        else:
            decision = "KEEP COLLECTING"
            reasons = _build_keep_reasons(wr, breakeven_wr, total_pnl_cents,
                                          w_lower, settled_count)
    elif settled_count >= 30:
        if wr / 100 < breakeven_wr and total_pnl_cents < 0:
            decision = "KILL"
            reasons = []
            reasons.append(f"WR {wr:.1f}% < BE {breakeven_wr*100:.1f}%")
            reasons.append(f"PnL {format_dollars(total_pnl_cents)}")
            reasons.append(f"n={settled_count} >= 30")
        else:
            decision = "KEEP COLLECTING"
            reasons = _build_keep_reasons(wr, breakeven_wr, total_pnl_cents,
                                          w_lower, settled_count)
    else:
        decision = "KEEP COLLECTING"
        reasons = [f"n={settled_count} < 30 (need more data)"]

    return {
        "total": total,
        "settled": settled_count,
        "pending": pending_count,
        "wins": wins,
        "losses": losses,
        "wr": wr,
        "avg_price": avg_price,
        "avg_edge": avg_edge,
        "avg_stc": avg_stc,
        "pnl_cents": total_pnl_cents,
        "breakeven_wr": breakeven_wr,
        "margin": margin,
        "wilson_lower": w_lower,
        "decision": decision,
        "reasons": reasons,
    }


def _build_keep_reasons(wr, breakeven_wr, total_pnl_cents, w_lower, settled_count):
    reasons = []
    if settled_count < 50:
        reasons.append(f"n={settled_count} < 50")
    if wr / 100 <= breakeven_wr + 0.02:
        reasons.append(f"WR {wr:.1f}% not > BE+2pp ({breakeven_wr*100:.1f}%+2)")
    if total_pnl_cents <= 0:
        reasons.append(f"PnL {format_dollars(total_pnl_cents)} <= 0")
    if w_lower <= breakeven_wr:
        reasons.append(f"Wilson lower {w_lower*100:.1f}% <= BE {breakeven_wr*100:.1f}%")
    if not reasons:
        reasons.append("Promising but not all criteria met")
    return reasons


def main():
    parser = argparse.ArgumentParser(description="Shadow strategy evaluator")
    parser.add_argument("--db", default="../state.db", help="Path to state.db")
    parser.add_argument("--days", type=int, default=14, help="Lookback days")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"ERROR: Database not found at {db_path.resolve()}")
        sys.exit(1)

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA busy_timeout=10000")
    conn.row_factory = sqlite3.Row

    cutoff = (datetime.now(timezone.utc) - timedelta(days=args.days)).isoformat()

    # ── Discover all shadow strategies ──────────────────────────────────────
    stages_sql = """
        SELECT DISTINCT filter_stage
        FROM evaluated_opportunities
        WHERE evaluation_time >= ?
        ORDER BY filter_stage
    """
    all_stages = [r["filter_stage"] for r in conn.execute(stages_sql, (cutoff,))]

    shadow_stages = []
    for s in all_stages:
        if s in NON_SHADOW_STAGES:
            continue
        # Include known shadow stages + anything else that's not non-shadow
        shadow_stages.append(s)

    if not shadow_stages:
        print(f"No shadow strategies found in the last {args.days} days.")
        conn.close()
        sys.exit(0)

    # ── Load data per strategy ──────────────────────────────────────────────
    fetch_sql = """
        SELECT filter_stage, market_price, market_result, status,
               counterfactual_pnl, position_size, evaluation_time,
               edge, seconds_to_close, side
        FROM evaluated_opportunities
        WHERE filter_stage = ?
          AND evaluation_time >= ?
    """

    results = {}
    for stage in shadow_stages:
        rows = conn.execute(fetch_sql, (stage, cutoff)).fetchall()
        if rows:
            results[stage] = evaluate_strategy(rows)

    conn.close()

    if not results:
        print(f"No shadow data found in the last {args.days} days.")
        sys.exit(0)

    # ── Hourly Alt Shadow Strategies (separate table) ───────────────────────
    hourly_alt_results = _evaluate_hourly_alt_shadows(conn, cutoff)
    spx_harrv_results = _evaluate_spx_harrv_shadow(conn, cutoff)

    # ── Sort by PnL descending ──────────────────────────────────────────────
    sorted_stages = sorted(results.keys(),
                           key=lambda s: results[s]["pnl_cents"],
                           reverse=True)

    # ── Print report ────────────────────────────────────────────────────────
    print(f"# Shadow Strategy Evaluation Report")
    print(f"**Lookback:** {args.days} days (since {cutoff[:10]})")
    print(f"**Strategies found:** {len(sorted_stages)}")
    print()

    # Summary table
    print("## Summary")
    print()
    print("| Strategy | Signals | Settled | WR% | BE WR% | Margin | PnL | Decision |")
    print("|----------|---------|---------|-----|--------|--------|-----|----------|")
    for stage in sorted_stages:
        r = results[stage]
        margin_str = f"{r['margin']*100:+.1f}pp"
        print(f"| {stage} | {r['total']} | {r['settled']} | "
              f"{r['wr']:.1f}% | {r['breakeven_wr']*100:.1f}% | "
              f"{margin_str} | {format_dollars(r['pnl_cents'])} | "
              f"**{r['decision']}** |")
    print()

    # Per-strategy detail
    print("## Per-Strategy Details")
    print()
    for stage in sorted_stages:
        r = results[stage]
        print(f"### {stage}")
        print()
        print(f"**Overview:** {r['total']} signals "
              f"({r['settled']} settled, {r['pending']} pending)")
        print()
        print(f"**Performance:**")
        print(f"- Record: {r['wins']}W / {r['losses']}L "
              f"({r['wr']:.1f}% WR)")
        print(f"- Counterfactual PnL: {format_dollars(r['pnl_cents'])}")
        print(f"- Avg price: {r['avg_price']:.1f}c | "
              f"Avg edge: {r['avg_edge']:.2f}% | "
              f"Avg STC: {r['avg_stc']:.0f}s")
        print()
        print(f"**Breakeven Analysis:**")
        print(f"- Breakeven WR: {r['breakeven_wr']*100:.1f}%")
        print(f"- Margin: {r['margin']*100:+.1f}pp "
              f"{'(>2pp OK)' if r['margin'] > 0.02 else '(<2pp insufficient)'}")
        print()
        print(f"**Statistical Confidence:**")
        print(f"- Wilson 95% CI lower: {r['wilson_lower']*100:.1f}%")
        if r["settled"] > 0:
            above = r["wilson_lower"] > r["breakeven_wr"]
            print(f"- Lower > breakeven? "
                  f"{'YES — strong signal' if above else 'NO — not yet significant'}")
        print(f"- Sample size: {r['settled']} "
              f"{'(>= 50 OK)' if r['settled'] >= 50 else '(< 50 — need more)'}")
        print()
        print(f"**Decision: {r['decision']}**")
        for reason in r["reasons"]:
            print(f"  - {reason}")
        print()

    # ── Hourly Shadow Strategies Section ─────────────────────────────────────
    _print_hourly_shadow_section(hourly_alt_results, spx_harrv_results)

    # ── Promotion candidates highlight ──────────────────────────────────────
    promotes = [s for s in sorted_stages if results[s]["decision"] == "PROMOTE"]
    kills = [s for s in sorted_stages if results[s]["decision"] == "KILL"]
    if promotes:
        print("## Action Items: PROMOTE")
        for s in promotes:
            r = results[s]
            print(f"- **{s}**: {r['wr']:.1f}% WR, "
                  f"{format_dollars(r['pnl_cents'])} PnL, "
                  f"n={r['settled']}")
        print()
    if kills:
        print("## Action Items: KILL")
        for s in kills:
            r = results[s]
            print(f"- **{s}**: {r['wr']:.1f}% WR, "
                  f"{format_dollars(r['pnl_cents'])} PnL, "
                  f"n={r['settled']}")
        print()


def _evaluate_hourly_alt_shadows(conn: sqlite3.Connection, cutoff: str) -> dict:
    """Evaluate hourly alt shadow strategies (MM + HAR-RV) from their own table."""
    results = {}
    try:
        conn.execute("SELECT 1 FROM hourly_alt_shadow_signals LIMIT 1")
    except Exception:
        return results  # Table doesn't exist

    for strategy in ("mm_shadow", "harrv_shadow"):
        rows = conn.execute("""
            SELECT asset, status, market_result, shadow_pnl_cents,
                   shadow_contracts, market_price, edge, seconds_to_close,
                   mm_buy_filled, mm_sell_filled
            FROM hourly_alt_shadow_signals
            WHERE strategy=? AND evaluation_time >= ?
        """, (strategy, cutoff)).fetchall()
        if not rows:
            continue

        # Group by asset
        by_asset = {}
        for r in rows:
            a = r["asset"]
            if a not in by_asset:
                by_asset[a] = []
            by_asset[a].append(r)

        for asset, asset_rows in by_asset.items():
            settled = [r for r in asset_rows if r["status"] == "settled"]
            pending = [r for r in asset_rows if r["status"] == "pending"]
            wins = sum(1 for r in settled if r["market_result"] in ("yes", "all_yes"))
            losses = len(settled) - wins
            wr = wins / len(settled) * 100 if settled else 0.0

            pnl = sum(r["shadow_pnl_cents"] or 0 for r in settled)
            prices = [r["market_price"] for r in asset_rows if r["market_price"]]
            avg_price = sum(prices) / len(prices) if prices else 0.0
            edges = [r["edge"] for r in asset_rows if r["edge"] is not None]
            avg_edge = sum(edges) / len(edges) if edges else 0.0
            stcs = [r["seconds_to_close"] for r in asset_rows if r["seconds_to_close"]]
            avg_stc = sum(stcs) / len(stcs) if stcs else 0.0

            # MM fill rate
            fill_rate = None
            if strategy == "mm_shadow":
                total_mm = len(settled)
                filled = sum(1 for r in settled if r["mm_buy_filled"])
                fill_rate = filled / total_mm * 100 if total_mm > 0 else 0.0

            breakeven_wr = avg_price / 100 if avg_price > 0 else 0.5
            margin = (wr / 100) - breakeven_wr
            w_lower = wilson_lower(wins, len(settled)) if settled else 0.0

            # Decision
            n = len(settled)
            if n >= 50 and wr / 100 > breakeven_wr + 0.02 and pnl > 0 and w_lower > breakeven_wr:
                decision = "PROMOTE"
            elif n >= 30 and wr / 100 < breakeven_wr and pnl < 0:
                decision = "KILL"
            else:
                decision = "KEEP COLLECTING"

            label = f"hourly_{strategy.replace('_shadow', '')}_{asset}"
            results[label] = {
                "strategy": strategy,
                "asset": asset,
                "total": len(asset_rows),
                "settled": n,
                "pending": len(pending),
                "wins": wins,
                "losses": losses,
                "wr": wr,
                "avg_price": avg_price,
                "avg_edge": avg_edge,
                "avg_stc": avg_stc,
                "pnl_cents": pnl,
                "breakeven_wr": breakeven_wr,
                "margin": margin,
                "wilson_lower": w_lower,
                "fill_rate": fill_rate,
                "decision": decision,
            }

    return results


def _evaluate_spx_harrv_shadow(conn: sqlite3.Connection, cutoff: str) -> dict:
    """Evaluate SPX HAR-RV shadow from its own table."""
    results = {}
    try:
        conn.execute("SELECT 1 FROM spx_harrv_shadow_signals LIMIT 1")
    except Exception:
        return results  # Table doesn't exist

    rows = conn.execute("""
        SELECT status, market_result, shadow_pnl_cents,
               shadow_contracts, market_price, edge, seconds_to_close,
               gates_passed, har_method
        FROM spx_harrv_shadow_signals
        WHERE evaluation_time >= ?
    """, (cutoff,)).fetchall()
    if not rows:
        return results

    settled = [r for r in rows if r["status"] == "settled"]
    pending = [r for r in rows if r["status"] == "pending"]
    wins = sum(1 for r in settled if r["market_result"] in ("yes", "all_yes"))
    losses = len(settled) - wins
    wr = wins / len(settled) * 100 if settled else 0.0

    pnl = sum(r["shadow_pnl_cents"] or 0 for r in settled)
    prices = [r["market_price"] for r in rows if r["market_price"]]
    avg_price = sum(prices) / len(prices) if prices else 0.0
    edges = [r["edge"] for r in rows if r["edge"] is not None]
    avg_edge = sum(edges) / len(edges) if edges else 0.0
    stcs = [r["seconds_to_close"] for r in rows if r["seconds_to_close"]]
    avg_stc = sum(stcs) / len(stcs) if stcs else 0.0

    # OLS fitting status
    methods = [r["har_method"] for r in rows if r["har_method"]]
    ols_count = sum(1 for m in methods if m == "ols")
    prior_count = sum(1 for m in methods if m == "prior")

    gated_in = sum(1 for r in settled if r["shadow_contracts"] and r["shadow_contracts"] > 0)
    gated_out = len(settled) - gated_in

    breakeven_wr = avg_price / 100 if avg_price > 0 else 0.5

    results["spx_harrv"] = {
        "total": len(rows),
        "settled": len(settled),
        "pending": len(pending),
        "wins": wins,
        "losses": losses,
        "wr": wr,
        "avg_price": avg_price,
        "avg_edge": avg_edge,
        "avg_stc": avg_stc,
        "pnl_cents": pnl,
        "breakeven_wr": breakeven_wr,
        "margin": (wr / 100) - breakeven_wr,
        "wilson_lower": wilson_lower(wins, len(settled)) if settled else 0.0,
        "ols_count": ols_count,
        "prior_count": prior_count,
        "gated_in": gated_in,
        "gated_out": gated_out,
        "decision": "KEEP COLLECTING",
    }

    return results


def _print_hourly_shadow_section(hourly_results: dict, spx_results: dict):
    """Print the hourly shadow strategies section of the report."""
    if not hourly_results and not spx_results:
        return

    print()
    print("=" * 70)
    print("# Hourly Shadow Strategies")
    print("=" * 70)
    print()

    # ── Crypto Hourly ──
    crypto_keys = [k for k in sorted(hourly_results.keys())]
    if crypto_keys:
        print("## Crypto Hourly Shadows")
        print()
        print("| Strategy | Asset | Settled | WR% | BE% | Margin | PnL | Fill% | Decision |")
        print("|----------|-------|---------|-----|-----|--------|-----|-------|----------|")
        for key in crypto_keys:
            r = hourly_results[key]
            fill_str = f"{r['fill_rate']:.0f}%" if r["fill_rate"] is not None else "—"
            margin_str = f"{r['margin']*100:+.1f}pp"
            print(f"| {key} | {r['asset']} | {r['settled']} | "
                  f"{r['wr']:.1f}% | {r['breakeven_wr']*100:.1f}% | "
                  f"{margin_str} | {format_dollars(r['pnl_cents'])} | "
                  f"{fill_str} | **{r['decision']}** |")
        print()

        # Per-asset routing recommendation
        print("### Per-Asset Routing Recommendation")
        print()
        for asset in ("BTC", "ETH", "SOL", "XRP"):
            asset_keys = [k for k in crypto_keys if hourly_results[k]["asset"] == asset]
            if not asset_keys:
                print(f"- **{asset}**: Not in shadow strategies")
                continue
            best = max(asset_keys, key=lambda k: hourly_results[k]["pnl_cents"])
            r = hourly_results[best]
            if r["decision"] == "PROMOTE":
                print(f"- **{asset}**: PROMOTE {best} "
                      f"({r['wr']:.1f}% WR, {format_dollars(r['pnl_cents'])}, n={r['settled']})")
            elif r["settled"] >= 20 and r["pnl_cents"] > 0:
                print(f"- **{asset}**: Promising — {best} "
                      f"({r['wr']:.1f}% WR, {format_dollars(r['pnl_cents'])}, "
                      f"n={r['settled']}, need {max(0, 50 - r['settled'])} more)")
            elif r["settled"] >= 20 and r["pnl_cents"] < 0:
                print(f"- **{asset}**: Underperforming — best is {best} "
                      f"({r['wr']:.1f}% WR, {format_dollars(r['pnl_cents'])})")
            else:
                print(f"- **{asset}**: Insufficient data (n={r['settled']})")
        print()

    # ── SPX Hourly ──
    if spx_results:
        print("## SPX Hourly Shadow (Separate Track)")
        print()
        for key, r in spx_results.items():
            print(f"### {key}")
            print(f"- Settled: {r['settled']} ({r['wins']}W / {r['losses']}L, {r['wr']:.1f}% WR)")
            print(f"- PnL: {format_dollars(r['pnl_cents'])}")
            print(f"- Avg price: {r['avg_price']:.1f}c | Avg edge: {r['avg_edge']*100:.2f}%")
            print(f"- HAR method: {r['ols_count']} OLS / {r['prior_count']} prior "
                  f"{'(OLS fitting active)' if r['ols_count'] > 0 else '(still on priors)'}")
            print(f"- Gates: {r['gated_in']} passed / {r['gated_out']} blocked")
            print(f"- Decision: **{r['decision']}**")
            print()


if __name__ == "__main__":
    main()
