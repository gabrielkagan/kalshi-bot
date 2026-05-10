#!/usr/bin/env python3
"""Comprehensive 15M opportunity & shadow analysis.

Rewritten 2026-05-05 to address structural bugs in the prior script:
- SHADOW_STAGES hardcoded to 6 stages (prod has 25+) → ~95% of shadow
  volume invisible. Now dynamic via classify_stage().
- Cell-block stages (TM98_BLEED, SOL_TAKER_BLEED, tm96_calmlp_gate_blocked,
  HPSB) deflated `filter_stage='candidate'` rollups per CLAUDE.md. Now a
  separate BLOCK tier.
- Win rate computed YES-side-only — NO-side shadows had inverted WR. Now
  side-aware via is_win().
- Promotion gate omitted Wilson lower CI check. Now enforced.

Note: counterfactual_pnl IS net of taker fee + side-aware + Kelly-sized
in source (bot.py:25721, 25731-25736). Sum it directly.

Usage:
    scp botuser@<vps>:~/kalshi-bot-repo/state.db /tmp/state.db
    python3 scripts/alpha_audit.py --db /tmp/state.db
    python3 scripts/alpha_audit.py --db /tmp/state.db --days 7 --asset BTC
"""

import argparse
import math
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional


# ── Stage classification ────────────────────────────────────────────────

HARD_REJECT_STAGES = frozenset({
    # Pre-evaluation hard stops (in current bot.py).
    "insufficient_edge", "price_out_of_range", "zero_sizing",
    "silent_loss_cooldown", "silent_vol_none", "silent_spot_none",
    "dead_hour_passed",
    "low_probability", "no_best_ask", "no_orderbook",
    "overnight_lp_vol_skip", "single_asset_selection",
    "strategy_wait", "threshold_implausible",
})

KNOWN_BLOCK_STAGES = frozenset({
    "TM98_97_98C_2_5MIN_BLEED",
    "SOL_TAKER_85_89C_2_5MIN_BLEED",
    "SOL_BLEED_V2_88_93C_2_5MIN",
    "96C_SOL_XRP_STC_DANGER_BAND",
    "tm96_calmlp_gate_blocked",
})

# Explicit classifications for stages that don't match the heuristic but are
# definitively known. Heuristic still handles new stages; this dict enforces
# correctness for stages with non-obvious names.
EXPLICIT_STAGE_CLASSIFICATIONS = {
    "terminal_momentum": "SHADOW",
    "weekend_discount": "SHADOW",
    "overnight_discount": "SHADOW",
    "sol_usmorn_sub88": "SHADOW",
    "sol_low_entry_high_stc": "SHADOW",
    "usaft_short_stc": "HARD_REJECT",
    "hourly_live": "CANDIDATE",  # 15M filter excludes via product_type, defensive
}

# Regime-change cutoffs the user may want to gate on. See `git log` /
# CLAUDE.md "Bleed-cell blocks LIVE — May 1" for the most recent material
# config change to 15M behavior. Used for top-of-output regime banner.
REGIME_CUTOFFS = [
    ("2026-04-30T16:16:00", "Bleed-cell blocks activated (TM98 + SOL_TAKER + tm96_calmlp_gate)"),
]

# Filters used in every section to keep 15M-scoped only.
EVAL_15M_FILTER = (
    "AND (product_type IS NULL OR product_type NOT IN "
    "('hourly','weather','sports','spx_hourly'))"
)
SETTLED_15M_FILTER = "AND event_ticker NOT LIKE '%D-%'"

# Side-aware win expression for settled_trades. NO opps win on
# market_result='no'/'all_no'; YES opps win on 'yes'/'all_yes'. Drop-in
# replacement for the old YES-only `CASE WHEN market_result='yes' ...`
# pattern. Uses COALESCE because some legacy rows have side=NULL (treat
# as YES — the historical default before the side column was added).
SIDE_AWARE_WIN_SQL = (
    "(CASE WHEN (COALESCE(side,'yes')='yes' AND market_result IN ('yes','all_yes')) "
    "       OR (side='no' AND market_result IN ('no','all_no')) "
    "      THEN 1 ELSE 0 END)"
)


def classify_stage(stage: Optional[str]) -> str:
    """Return tier for a filter_stage value:
    CANDIDATE / SHADOW / BLOCK / HARD_REJECT / UNKNOWN.

    Heuristic for forward-compat: stages ending in `_shadow` or starting
    with `decided_contract`/`dc_shadow` → SHADOW; ALL-CAPS with BLEED /
    DANGER substring or `_blocked` suffix → BLOCK. Unknown stages return
    UNKNOWN so they surface in the funnel rather than silently disappear.
    """
    if not stage:
        return "UNKNOWN"
    if stage == "candidate":
        return "CANDIDATE"
    if stage in EXPLICIT_STAGE_CLASSIFICATIONS:
        return EXPLICIT_STAGE_CLASSIFICATIONS[stage]
    if stage in HARD_REJECT_STAGES:
        return "HARD_REJECT"
    if stage in KNOWN_BLOCK_STAGES:
        return "BLOCK"
    s = stage
    if (s.endswith("_shadow")
            or s.startswith("decided_contract")
            or s.startswith("dc_shadow")
            or s.startswith("dc_t")
            or "shadow" in s.lower()):
        return "SHADOW"
    if "BLEED" in s or "DANGER" in s or s.endswith("_blocked"):
        return "BLOCK"
    return "UNKNOWN"


def is_win(side: Optional[str], market_result: Optional[str]) -> bool:
    """Side-aware win check. NO opps win on 'no'/'all_no'."""
    s = (side or "yes").lower()
    r = (market_result or "").lower()
    if s == "no":
        return r in ("no", "all_no")
    return r in ("yes", "all_yes")


def wilson_lower(wins: int, n: int, z: float = 1.96) -> float:
    """Wilson 95% lower-bound. n<=0 → 0.0."""
    if n <= 0:
        return 0.0
    p = wins / n
    den = 1 + z * z / n
    centre = p + z * z / (2 * n)
    spread = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n)
    return max(0.0, (centre - spread) / den)


def taker_fee_cents(contracts: int, price_cents: int) -> int:
    p = price_cents / 100.0
    return math.ceil(0.07 * contracts * p * (1 - p))


def breakeven_wr(price_cents: int) -> float:
    fee = taker_fee_cents(1, price_cents)
    return (price_cents + fee) / 100.0


def fmt_pnl(cents: float) -> str:
    if not cents:
        return "$0.00"
    return f"${cents/100:+.2f}"


def fmt_pct(val: float) -> str:
    return f"{val*100:.1f}%"


# ── Section computations (importable / testable) ───────────────────────


def compute_funnel(conn: sqlite3.Connection, since: str,
                   asset_filter: str) -> dict:
    """Per-tier counts + per-stage detail. Sums to 100% of evals."""
    rows = conn.execute(f"""
        SELECT filter_stage, COUNT(*) AS n,
               SUM(CASE WHEN order_id IS NOT NULL THEN 1 ELSE 0 END) AS filled,
               SUM(CASE WHEN order_outcome IS NOT NULL THEN 1 ELSE 0 END) AS submitted
        FROM evaluated_opportunities
        WHERE evaluation_time >= ?
        {EVAL_15M_FILTER}
        {asset_filter}
        GROUP BY filter_stage
    """, (since,)).fetchall()

    by_tier: defaultdict = defaultdict(int)
    by_stage = []
    candidate_filled = 0
    candidate_submitted = 0
    for r in rows:
        tier = classify_stage(r["filter_stage"])
        by_tier[tier] += r["n"]
        by_stage.append({
            "stage": r["filter_stage"],
            "tier": tier,
            "n": r["n"],
            "filled": r["filled"] or 0,
            "submitted": r["submitted"] or 0,
        })
        if r["filter_stage"] == "candidate":
            candidate_filled = r["filled"] or 0
            candidate_submitted = r["submitted"] or 0
    return {
        "by_tier": dict(by_tier),
        "by_stage": by_stage,
        "total": sum(by_tier.values()),
        "candidate_filled": candidate_filled,
        "candidate_submitted": candidate_submitted,
    }


def compute_shadows(conn: sqlite3.Connection, since: str,
                    asset_filter: str) -> list:
    """Per-shadow-stage rollup. Side-aware WR + Wilson + promotion verdict.

    Includes both SHADOW and UNKNOWN tiers (UNKNOWN surfaces new stages
    that haven't been categorized yet — better to over-include than to
    silently drop them).
    """
    stages = conn.execute(f"""
        SELECT filter_stage, COUNT(*) AS n
        FROM evaluated_opportunities
        WHERE evaluation_time >= ?
        {EVAL_15M_FILTER}
        {asset_filter}
        GROUP BY filter_stage
    """, (since,)).fetchall()

    shadows = []
    for srow in stages:
        stage = srow["filter_stage"]
        tier = classify_stage(stage)
        if tier not in ("SHADOW", "UNKNOWN"):
            continue
        detail = conn.execute(f"""
            SELECT side, market_result, counterfactual_pnl, position_size,
                   market_price, status
            FROM evaluated_opportunities
            WHERE filter_stage = ?
              AND evaluation_time >= ?
              {EVAL_15M_FILTER}
              {asset_filter}
        """, (stage, since)).fetchall()
        settled = [d for d in detail if d["status"] == "settled"]
        wins = sum(1 for d in settled if is_win(d["side"], d["market_result"]))
        settled_n = len(settled)
        losses = settled_n - wins
        wr = wins / settled_n if settled_n else 0.0
        # avg_price MUST be computed from settled rows so it's denominator-
        # consistent with WR and breakeven gating. Round-6 review: prior
        # version averaged across `detail` (incl. pending), so a stage with
        # 50 settled @88c + 100 pending @50c would compute be(62c) ≈ 0.63,
        # then accept WR 90% as "PROMOTE" when correct gate is be(88c) ≈ 0.89.
        settled_prices = [d["market_price"] for d in settled
                          if d["market_price"] is not None]
        avg_price = (sum(settled_prices) / len(settled_prices)
                     if settled_prices else 0.0)
        be = breakeven_wr(int(round(avg_price))) if avg_price > 0 else 0.5
        wlow = wilson_lower(wins, settled_n)
        pnl_cents = sum(d["counterfactual_pnl"] or 0 for d in settled)
        # size_basis: 1ct_sim if majority of SETTLED rows have NULL or 1-ct
        # sizing. Round-5 review: prior version counted from `detail` (all
        # rows incl. pending) but compared against settled_n/2, which could
        # tag a thin-sizing settled cohort as 'kelly' if pending rows had
        # sizing.
        sized = sum(1 for d in settled
                    if d["position_size"] and d["position_size"] > 1)
        size_basis = "kelly" if (settled_n and sized > settled_n / 2) else "1ct_sim"
        verdict = "KEEP"
        if settled_n >= 50:
            if (wr > be + 0.02) and pnl_cents > 0 and wlow > be:
                verdict = "PROMOTE"
            elif wr < be and pnl_cents < 0:
                verdict = "KILL"
        shadows.append({
            "stage": stage,
            "tier": tier,
            "n": srow["n"],
            "settled": settled_n,
            "wins": wins,
            "losses": losses,
            "wr": wr,
            "avg_price": avg_price,
            "breakeven_wr": be,
            "wilson_lower": wlow,
            "pnl_cents": pnl_cents,
            "pnl_source": "cf_pnl",
            "size_basis": size_basis,
            "verdict": verdict,
        })
    # Sort PROMOTE → top, then by pnl_cents desc.
    shadows.sort(key=lambda s: (s["verdict"] == "PROMOTE",
                                s["pnl_cents"]),
                 reverse=True)
    return shadows


def compute_block_effectiveness(conn: sqlite3.Connection, since: str,
                                asset_filter: str) -> list:
    """Per-cell-block stage: foregone PnL + disaster catch rate.

    `disasters_caught`: settled + market_result IN (no, all_no) for YES side
    (or yes/all_yes for NO side) AND cf_pnl < -1000c (~$10 loss). Heuristic
    threshold for "disaster" — tune as the cell-block program matures.
    """
    stages = conn.execute(f"""
        SELECT filter_stage, COUNT(*) AS n
        FROM evaluated_opportunities
        WHERE evaluation_time >= ?
        {EVAL_15M_FILTER}
        {asset_filter}
        GROUP BY filter_stage
    """, (since,)).fetchall()

    blocks = []
    for srow in stages:
        stage = srow["filter_stage"]
        if classify_stage(stage) != "BLOCK":
            continue
        detail = conn.execute(f"""
            SELECT side, market_result, counterfactual_pnl, position_size,
                   market_price, status, strategy
            FROM evaluated_opportunities
            WHERE filter_stage = ?
              AND evaluation_time >= ?
              {EVAL_15M_FILTER}
              {asset_filter}
        """, (stage, since)).fetchall()
        settled = [d for d in detail if d["status"] == "settled"]
        n_settled = len(settled)
        foregone_pnl = sum(d["counterfactual_pnl"] or 0 for d in settled)
        wins = sum(1 for d in settled if is_win(d["side"], d["market_result"]))
        losses = n_settled - wins
        # Disasters: would-have-lost AND cf_pnl is materially negative.
        disasters = sum(
            1 for d in settled
            if (not is_win(d["side"], d["market_result"]))
            and (d["counterfactual_pnl"] or 0) < -1000
        )
        # avg_price from settled only for denominator consistency with the
        # win/disaster counts (round-6 review).
        settled_prices = [d["market_price"] for d in settled
                          if d["market_price"] is not None]
        avg_price = (sum(settled_prices) / len(settled_prices)
                     if settled_prices else 0.0)
        strategies = sorted({d["strategy"] for d in detail if d["strategy"]})
        blocks.append({
            "stage": stage,
            "n": srow["n"],
            "settled": n_settled,
            "wins_blocked": wins,
            "losses_blocked": losses,
            "disasters_caught": disasters,
            "foregone_pnl_cents": foregone_pnl,
            "avg_price": avg_price,
            "strategies": strategies,
        })
    blocks.sort(key=lambda b: b["foregone_pnl_cents"])
    return blocks


def compute_top_opportunities(conn: sqlite3.Connection, since: str,
                              asset_filter: str, days: int,
                              balance_cents: Optional[int] = None) -> list:
    """Rank opportunities by daily $ impact. Significance + sanity gated.

    Sanity gates beyond the 4 promotion criteria:
    - daily_cents > balance × 0.10 → tag IMPLAUSIBLE_FILL (cf_pnl assumes
      100% fill at recorded ask; high-volume shadows like low_price_shadow
      avg 147ct will not actually fill that depth without slippage).
    Verdict downgraded to PROMOTE_WITH_FILL_RISK when sanity gate trips.
    """
    shadows = compute_shadows(conn, since, asset_filter)
    if balance_cents is None:
        bal_row = conn.execute(f"""
            SELECT available_balance_cents FROM evaluated_opportunities
            WHERE evaluation_time >= ?
              AND available_balance_cents IS NOT NULL
              AND available_balance_cents > 0
              {EVAL_15M_FILTER}
            ORDER BY evaluation_time DESC LIMIT 1
        """, (since,)).fetchone()
        balance_cents = bal_row["available_balance_cents"] if bal_row else None
    # Treat 0 / None / negative as "unknown" so callers see a flag rather
    # than silently skipping the IMPLAUSIBLE_FILL gate (round-2 review).
    balance_unknown = (balance_cents is None) or (balance_cents <= 0)
    opps = []
    for s in shadows:
        if s["settled"] < 50:
            continue
        if s["wilson_lower"] <= s["breakeven_wr"]:
            continue
        if s["pnl_cents"] <= 0:
            continue
        daily = s["pnl_cents"] / days if days > 0 else 0
        flags = []
        verdict = s["verdict"]
        if balance_unknown:
            flags.append("BALANCE_UNKNOWN: IMPLAUSIBLE_FILL gate skipped (no recent balance row)")
        elif daily > balance_cents * 0.10:
            flags.append(
                f"IMPLAUSIBLE_FILL: daily {fmt_pnl(daily)} > 10% × bal "
                f"{fmt_pnl(balance_cents)} (cf_pnl assumes 100% fill; "
                f"slippage/depth not modeled)"
            )
            verdict = "PROMOTE_WITH_FILL_RISK"
        if s["size_basis"] == "1ct_sim":
            flags.append("UNSIZED: cf_pnl is 1ct sim, real Kelly impact unknown")
        opps.append({
            "kind": "shadow_promotion",
            "stage": s["stage"],
            "daily_cents": daily,
            "summary": (f"{s['stage']}: WR {fmt_pct(s['wr'])}, "
                        f"Wilson_lo {fmt_pct(s['wilson_lower'])} > "
                        f"BE {fmt_pct(s['breakeven_wr'])}, "
                        f"PnL {fmt_pnl(s['pnl_cents'])}, n={s['settled']}"),
            "verdict": verdict,
            "flags": flags,
        })
    opps.sort(key=lambda o: o["daily_cents"], reverse=True)
    return opps[:5]


# ── Settled-trades sections (kept from prior script — verified correct) ─


def compute_settled_by_asset(conn: sqlite3.Connection, since: str,
                             asset_filter: str) -> list:
    rows = conn.execute(f"""
        SELECT asset,
               COUNT(*) AS trades,
               SUM({SIDE_AWARE_WIN_SQL}) AS wins,
               SUM(pnl_cents - COALESCE(fee_cents, 0)) AS pnl,
               AVG(entry_price_cents) AS avg_price,
               AVG(count) AS avg_contracts
        FROM settled_trades
        WHERE settled_at >= ?
        {SETTLED_15M_FILTER}
        {asset_filter}
        GROUP BY asset
        ORDER BY SUM(pnl_cents - COALESCE(fee_cents, 0)) DESC
    """, (since,)).fetchall()
    return [dict(r) for r in rows]


def compute_settled_by_stc(conn: sqlite3.Connection, since: str,
                           asset_filter: str) -> list:
    rows = conn.execute(f"""
        SELECT
            CASE
                WHEN seconds_to_close < 100 THEN '0-100'
                WHEN seconds_to_close < 200 THEN '100-200'
                WHEN seconds_to_close < 300 THEN '200-300'
                WHEN seconds_to_close < 500 THEN '300-500'
                WHEN seconds_to_close < 900 THEN '500-900'
                ELSE '900+'
            END AS stc_bucket,
            COUNT(*) AS trades,
            SUM({SIDE_AWARE_WIN_SQL}) AS wins,
            SUM(pnl_cents - COALESCE(fee_cents, 0)) AS pnl
        FROM settled_trades
        WHERE settled_at >= ?
        AND seconds_to_close IS NOT NULL
        {SETTLED_15M_FILTER}
        {asset_filter}
        GROUP BY stc_bucket
        ORDER BY MIN(seconds_to_close)
    """, (since,)).fetchall()
    return [dict(r) for r in rows]


def compute_capital_utilization(conn: sqlite3.Connection, since: str,
                                asset_filter: str, days: int) -> dict:
    row = conn.execute(f"""
        SELECT COUNT(*) AS trades,
               AVG(count) AS avg_contracts,
               AVG(count * entry_price_cents) AS avg_deployed_cents
        FROM settled_trades
        WHERE settled_at >= ?
        {SETTLED_15M_FILTER}
        {asset_filter}
    """, (since,)).fetchone()
    bal_row = conn.execute(f"""
        SELECT available_balance_cents
        FROM evaluated_opportunities
        WHERE evaluation_time >= ?
        AND available_balance_cents IS NOT NULL
        AND available_balance_cents > 0
        {EVAL_15M_FILTER}
        ORDER BY evaluation_time DESC LIMIT 1
    """, (since,)).fetchone()
    return {
        "trades": (row["trades"] if row else 0) or 0,
        "trades_per_day": ((row["trades"] or 0) / days) if (row and days > 0) else 0,
        "avg_contracts": (row["avg_contracts"] if row else 0) or 0,
        "avg_deployed_cents": (row["avg_deployed_cents"] if row else 0) or 0,
        "balance_cents": (bal_row["available_balance_cents"] if bal_row else None),
    }


def compute_weekend_split(conn: sqlite3.Connection, since: str,
                          asset_filter: str) -> list:
    rows = conn.execute(f"""
        SELECT
            CASE WHEN CAST(strftime('%w', settled_at) AS INTEGER) IN (0, 6)
                THEN 'Weekend' ELSE 'Weekday' END AS period,
            COUNT(*) AS trades,
            SUM({SIDE_AWARE_WIN_SQL}) AS wins,
            SUM(pnl_cents - COALESCE(fee_cents, 0)) AS pnl
        FROM settled_trades
        WHERE settled_at >= ?
        {SETTLED_15M_FILTER}
        {asset_filter}
        GROUP BY period
        ORDER BY period
    """, (since,)).fetchall()
    return [dict(r) for r in rows]


def compute_hardreject_by_price(conn: sqlite3.Connection, since: str,
                                asset_filter: str) -> list:
    """insufficient_edge by price band — counterfactual quality check.

    Side-aware: a NO opp wins on result='no'/'all_no'. Computed in Python
    via is_win() rather than SQL CASE so the side semantics live in one
    place. (Round-1 review: prior version was YES-only and inverted WR
    for any NO insufficient_edge rows.)
    """
    rows = conn.execute(f"""
        SELECT market_price, side, market_result, counterfactual_pnl, status
        FROM evaluated_opportunities
        WHERE evaluation_time >= ?
        AND filter_stage = 'insufficient_edge'
        AND market_price BETWEEN 86 AND 99
        {EVAL_15M_FILTER}
        {asset_filter}
    """, (since,)).fetchall()
    by_price: dict = defaultdict(lambda: {"cnt": 0, "settled": 0, "wins": 0, "pnl": 0})
    for r in rows:
        p = r["market_price"]
        b = by_price[p]
        b["cnt"] += 1
        if r["status"] == "settled":
            b["settled"] += 1
            if is_win(r["side"], r["market_result"]):
                b["wins"] += 1
            b["pnl"] += r["counterfactual_pnl"] or 0
    out = []
    for p in sorted(by_price):
        d = dict(by_price[p])
        d["market_price"] = p
        out.append(d)
    return out


# ── Rendering ──────────────────────────────────────────────────────────


def section(num: int, title: str) -> None:
    print(f"\n{'=' * 72}")
    print(f"  Section {num}: {title}")
    print(f"{'=' * 72}\n")


def render_funnel(funnel: dict) -> None:
    section(1, "Filter Funnel — by tier and stage")
    total = funnel["total"]
    if total == 0:
        print("  No evaluated opportunities in lookback window.")
        return
    print(f"  Total evaluated: {total}\n")
    print(f"  {'Tier':<14} {'Count':>8} {'Pct':>8}")
    print(f"  {'-'*14} {'-'*8} {'-'*8}")
    tier_order = ["CANDIDATE", "SHADOW", "BLOCK", "HARD_REJECT", "UNKNOWN"]
    for tier in tier_order:
        cnt = funnel["by_tier"].get(tier, 0)
        pct = cnt / total if total else 0
        print(f"  {tier:<14} {cnt:>8} {pct*100:>7.1f}%")
    if funnel["candidate_filled"] or funnel["candidate_submitted"]:
        cand = funnel["by_tier"].get("CANDIDATE", 0)
        print(f"\n  CANDIDATE breakdown: "
              f"{funnel['candidate_submitted']}/{cand} submitted, "
              f"{funnel['candidate_filled']}/{cand} filled")
    print(f"\n  {'Stage':<38} {'Tier':<12} {'Count':>7} {'Pct':>7}")
    print(f"  {'-'*38} {'-'*12} {'-'*7} {'-'*7}")
    for s in sorted(funnel["by_stage"], key=lambda x: -x["n"]):
        print(f"  {s['stage'][:37]:<38} {s['tier']:<12} "
              f"{s['n']:>7} {s['n']/total*100:>6.1f}%")


def render_hardreject_by_price(rows: list, days: int) -> None:
    section(2, "Hard-Reject Quality — insufficient_edge by price (86-99c)")
    if not rows:
        print("  No insufficient_edge rejections at 86-99c.")
        return
    print(f"  {'Price':>5} {'Count':>7} {'Settled':>8} {'Wins':>6} "
          f"{'WR%':>8} {'BE WR%':>8} {'CF PnL':>11} {'Daily':>11}")
    print(f"  {'-'*5} {'-'*7} {'-'*8} {'-'*6} {'-'*8} {'-'*8} {'-'*11} {'-'*11}")
    total_pnl = 0
    for r in rows:
        p = r["market_price"]
        settled = r["settled"] or 0
        wins = r["wins"] or 0
        wr = wins / settled if settled else 0
        be = breakeven_wr(p) if p else 0
        pnl = r["pnl"] or 0
        total_pnl += pnl
        daily = pnl / days if days > 0 else 0
        wr_str = fmt_pct(wr) if settled else "N/A"
        print(f"  {p:>4}c {r['cnt']:>7} {settled:>8} {wins:>6} "
              f"{wr_str:>8} {fmt_pct(be):>8} {fmt_pnl(pnl):>11} "
              f"{fmt_pnl(daily):>11}")
    print(f"\n  Total counterfactual PnL: {fmt_pnl(total_pnl)} "
          f"({fmt_pnl(total_pnl/days) if days > 0 else '$0'}/day)")


def render_settled_by_stc(rows: list) -> None:
    section(3, "Settled Trade WR by STC bucket")
    if not rows:
        print("  No settled trades with STC data.")
        return
    print(f"  {'STC Bucket':<22} {'Trades':>7} {'Wins':>6} "
          f"{'WR%':>8} {'PnL':>12}")
    print(f"  {'-'*22} {'-'*7} {'-'*6} {'-'*8} {'-'*12}")
    for r in rows:
        label = r["stc_bucket"]
        if label == "0-100":
            label = "0-100s ** RISK **"
        elif label == "500-900":
            label = "500-900s (shadow)"
        wr = (r["wins"] or 0) / r["trades"] if r["trades"] else 0
        print(f"  {label:<22} {r['trades']:>7} {r['wins'] or 0:>6} "
              f"{fmt_pct(wr):>8} {fmt_pnl(r['pnl'] or 0):>12}")


def render_settled_by_asset(rows: list) -> None:
    section(4, "Settled Trade Performance by Asset")
    if not rows:
        print("  No settled trades.")
        return
    print(f"  {'Asset':<6} {'Trades':>7} {'Wins':>6} {'WR%':>8} "
          f"{'PnL':>12} {'AvgPrice':>9} {'AvgCt':>7}")
    print(f"  {'-'*6} {'-'*7} {'-'*6} {'-'*8} {'-'*12} {'-'*9} {'-'*7}")
    tt = tw = tp = 0
    for r in rows:
        wr = (r["wins"] or 0) / r["trades"] if r["trades"] else 0
        tt += r["trades"]
        tw += r["wins"] or 0
        tp += r["pnl"] or 0
        print(f"  {r['asset'] or '?':<6} {r['trades']:>7} {r['wins'] or 0:>6} "
              f"{fmt_pct(wr):>8} {fmt_pnl(r['pnl'] or 0):>12} "
              f"{r['avg_price'] or 0:>8.0f}c {r['avg_contracts'] or 0:>7.1f}")
    if tt:
        print(f"  {'-'*6} {'-'*7} {'-'*6} {'-'*8} {'-'*12}")
        print(f"  {'TOTAL':<6} {tt:>7} {tw:>6} {fmt_pct(tw/tt):>8} {fmt_pnl(tp):>12}")


def render_capital_utilization(util: dict) -> None:
    section(5, "Capital Utilization")
    if not util["trades"]:
        print("  No settled trades.")
        return
    print(f"  Trades:                  {util['trades']}")
    print(f"  Trades/day:              {util['trades_per_day']:.1f}")
    print(f"  Avg contracts/trade:     {util['avg_contracts']:.1f}")
    print(f"  Avg capital/trade:       {fmt_pnl(util['avg_deployed_cents'])}")
    if util["balance_cents"]:
        u = util["avg_deployed_cents"] / util["balance_cents"]
        print(f"  Latest balance:          {fmt_pnl(util['balance_cents'])}")
        print(f"  Utilization per trade:   {fmt_pct(u)}")


def render_shadows(shadows: list) -> None:
    section(6, "Shadow Strategy Status — dynamic + side-aware + Wilson")
    if not shadows:
        print("  No shadow data in lookback window.")
        return
    print(f"  {'Stage':<34} {'n':>5} {'Settled':>8} {'WR%':>7} "
          f"{'Wlow%':>7} {'BE%':>6} {'PnL':>11} {'Size':>8} {'Verdict':>10}")
    print(f"  {'-'*34} {'-'*5} {'-'*8} {'-'*7} {'-'*7} {'-'*6} "
          f"{'-'*11} {'-'*8} {'-'*10}")
    for s in shadows:
        print(f"  {s['stage'][:33]:<34} "
              f"{s['n']:>5} {s['settled']:>8} "
              f"{fmt_pct(s['wr']):>7} {fmt_pct(s['wilson_lower']):>7} "
              f"{fmt_pct(s['breakeven_wr']):>6} "
              f"{fmt_pnl(s['pnl_cents']):>11} "
              f"{s['size_basis']:>8} {s['verdict']:>10}")


def render_blocks(blocks: list) -> None:
    section(7, "Cell-Block Effectiveness")
    if not blocks:
        print("  No cell-block stages in lookback window.")
        return
    print(f"  {'Stage':<38} {'n':>5} {'Settled':>8} "
          f"{'Wins':>6} {'Loss':>6} {'Disasters':>10} {'Foregone PnL':>14}")
    print(f"  {'-'*38} {'-'*5} {'-'*8} {'-'*6} {'-'*6} {'-'*10} {'-'*14}")
    for b in blocks:
        print(f"  {b['stage'][:37]:<38} {b['n']:>5} {b['settled']:>8} "
              f"{b['wins_blocked']:>6} {b['losses_blocked']:>6} "
              f"{b['disasters_caught']:>10} "
              f"{fmt_pnl(b['foregone_pnl_cents']):>14}")
    print("\n  Foregone PnL = sum of cf_pnl on blocked rows (positive = "
          "block cost net wins; negative = block saved net losses).")


def render_weekend(rows: list) -> None:
    section(8, "Weekend vs Weekday Performance")
    if not rows:
        print("  No settled trades.")
        return
    print(f"  {'Period':<10} {'Trades':>7} {'Wins':>6} {'WR%':>8} "
          f"{'PnL':>12} {'PnL/Trade':>12}")
    print(f"  {'-'*10} {'-'*7} {'-'*6} {'-'*8} {'-'*12} {'-'*12}")
    for r in rows:
        wr = (r["wins"] or 0) / r["trades"] if r["trades"] else 0
        per = (r["pnl"] or 0) / r["trades"] if r["trades"] else 0
        print(f"  {r['period']:<10} {r['trades']:>7} {r['wins'] or 0:>6} "
              f"{fmt_pct(wr):>8} {fmt_pnl(r['pnl'] or 0):>12} "
              f"{fmt_pnl(per):>12}")


def render_top_opportunities(opps: list) -> None:
    section(9, "Top Opportunities — significance-gated, ranked by daily $")
    if not opps:
        print("  No opportunities clearing significance gate "
              "(n>=50 + Wilson_lo > BE + PnL > 0).")
        return
    for i, o in enumerate(opps, 1):
        print(f"  {i}. [{o['verdict']}] {o['summary']}")
        print(f"     Daily impact: {fmt_pnl(o['daily_cents'])}/day")
        for flag in o.get("flags", []):
            print(f"     ⚠ {flag}")
        print()


def render_regime_banner(since_iso: str, now_iso: Optional[str] = None) -> None:
    """Surface regime cutoffs that fall inside the lookback window.

    A cutoff fires only if since_iso < cutoff < now_iso. The upper bound
    matters: if REGIME_CUTOFFS gains a future-dated entry, this prevents
    a phantom banner from appearing during pre-cutoff lookbacks. (Round-2
    review.)
    """
    if now_iso is None:
        now_iso = datetime.now(timezone.utc).isoformat()
    spans = []
    for cutoff_iso, label in REGIME_CUTOFFS:
        if since_iso < cutoff_iso < now_iso:
            spans.append((cutoff_iso, label))
    if not spans:
        return
    print("\n" + "─" * 72)
    print("  REGIME NOTE — lookback window crosses these config changes:")
    for cutoff, label in spans:
        print(f"    • {cutoff[:10]}: {label}")
    print("  Pre/post numbers may not be directly comparable. Pass "
          "--regime-cutoff <iso> to filter.")
    print("─" * 72)


# ── Main ───────────────────────────────────────────────────────────────


def connect_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.row_factory = sqlite3.Row
    return conn


def main():
    parser = argparse.ArgumentParser(description="15M alpha audit")
    parser.add_argument("--db", default="../state.db", help="Path to state.db")
    parser.add_argument("--days", type=int, default=14, help="Lookback days")
    parser.add_argument("--asset", type=str, default=None,
                        help="Filter to specific asset (BTC/ETH/SOL/XRP)")
    parser.add_argument("--regime-cutoff", type=str, default=None,
                        help="ISO timestamp; clamp `since` to this if newer "
                             "(e.g. '2026-04-30T16:16:00' for cell-block era)")
    args = parser.parse_args()

    try:
        conn = connect_db(args.db)
    except Exception as e:
        print(f"ERROR: cannot open {args.db}: {e}", file=sys.stderr)
        sys.exit(1)

    since = (datetime.now(timezone.utc) - timedelta(days=args.days)).isoformat()
    if args.regime_cutoff and args.regime_cutoff > since:
        since = args.regime_cutoff
    asset_filter = ""
    if args.asset:
        # Round-2 sanitization: asset must be alphabetic, length<=8.
        asset_in = args.asset.upper()
        if not (asset_in.isalpha() and len(asset_in) <= 8):
            print(f"ERROR: --asset must be alphabetic, got {args.asset!r}",
                  file=sys.stderr)
            sys.exit(2)
        asset_filter = f"AND asset = '{asset_in}'"

    print("=" * 72)
    print("  ALPHA AUDIT — Comprehensive Opportunity Analysis")
    print(f"  Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"  Lookback:  {args.days} days (since {since[:10]})")
    print(f"  Database:  {args.db}")
    if args.asset:
        print(f"  Asset:     {args.asset.upper()}")
    if args.regime_cutoff:
        print(f"  Regime:    >= {args.regime_cutoff}")
    print("=" * 72)
    if not args.regime_cutoff:
        render_regime_banner(since)

    funnel = compute_funnel(conn, since, asset_filter)
    render_funnel(funnel)

    rejq = compute_hardreject_by_price(conn, since, asset_filter)
    render_hardreject_by_price(rejq, args.days)

    stc_rows = compute_settled_by_stc(conn, since, asset_filter)
    render_settled_by_stc(stc_rows)

    asset_rows = compute_settled_by_asset(conn, since, asset_filter)
    render_settled_by_asset(asset_rows)

    util = compute_capital_utilization(conn, since, asset_filter, args.days)
    render_capital_utilization(util)

    shadows = compute_shadows(conn, since, asset_filter)
    render_shadows(shadows)

    blocks = compute_block_effectiveness(conn, since, asset_filter)
    render_blocks(blocks)

    weekend = compute_weekend_split(conn, since, asset_filter)
    render_weekend(weekend)

    opps = compute_top_opportunities(conn, since, asset_filter, args.days)
    render_top_opportunities(opps)

    print("\n" + "=" * 72)
    print("  Audit complete.")
    print("=" * 72)
    conn.close()


if __name__ == "__main__":
    main()
