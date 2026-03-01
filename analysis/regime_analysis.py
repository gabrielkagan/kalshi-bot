#!/usr/bin/env python3
"""
Reusable regime performance analysis for the Kalshi crypto trading bot.

Runs against state.db and outputs a comprehensive deep dive:
  Section 1: Performance summary (current regime)
  Section 2: Leak analysis (evaluated opportunities, counterfactual P&L)
  Section 3: Extended regime data (broader date ranges for statistical power)
  Section 4: Additional analysis (z-score, STC 300-600s, maker timing, fees, etc.)

Usage:
    # On VPS (default dates):
    python3 analysis/regime_analysis.py

    # Override regime start dates:
    python3 analysis/regime_analysis.py --narrow "2026-02-28T20:10:00Z" --medium "2026-02-27T00:00:00Z" --broad "2026-02-25T00:00:00Z"

    # Different database path:
    python3 analysis/regime_analysis.py --db /path/to/state.db

    # Run specific sections only:
    python3 analysis/regime_analysis.py --sections 1 2
"""

import argparse
import sqlite3
import sys


# ── Defaults ──────────────────────────────────────────────────────────────────
DEFAULT_DB = "state.db"
DEFAULT_BROAD = "2026-02-25T00:00:00Z"    # MIN_ENTRY=87 regime
DEFAULT_MEDIUM = "2026-02-27T00:00:00Z"   # Maker-only + sizing regime
DEFAULT_NARROW = "2026-02-28T20:10:00Z"   # Current full config


def get_args():
    p = argparse.ArgumentParser(description="Kalshi bot regime performance analysis")
    p.add_argument("--db", default=DEFAULT_DB, help="Path to state.db")
    p.add_argument("--narrow", default=DEFAULT_NARROW, help="Narrow regime start (current full config)")
    p.add_argument("--medium", default=DEFAULT_MEDIUM, help="Medium regime start (maker-only + sizing)")
    p.add_argument("--broad", default=DEFAULT_BROAD, help="Broad regime start (MIN_ENTRY=87)")
    p.add_argument("--sections", nargs="*", type=int, default=[1, 2, 3, 4],
                   help="Which sections to run (1-4)")
    return p.parse_args()


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 1: Performance Summary — Current Regime
# ═══════════════════════════════════════════════════════════════════════════════
def section_1(c, regime_start):
    print("=" * 80)
    print(f"SECTION 1: PERFORMANCE SUMMARY — Current Regime (since {regime_start})")
    print("=" * 80)

    r = c.execute("""
        SELECT COUNT(*) as n,
            SUM(CASE WHEN pnl_cents > 0 THEN 1 ELSE 0 END) as wins,
            SUM(CASE WHEN pnl_cents <= 0 THEN 1 ELSE 0 END) as losses,
            SUM(pnl_cents) as total_pnl,
            ROUND(AVG(pnl_cents), 1) as avg_pnl,
            SUM(fee_cents) as total_fees,
            ROUND(AVG(entry_price_cents), 1) as avg_entry,
            ROUND(AVG(seconds_to_close), 1) as avg_stc,
            ROUND(AVG(count), 1) as avg_contracts,
            MIN(entry_price_cents) as min_entry,
            MAX(entry_price_cents) as max_entry,
            SUM(count * entry_price_cents) as total_risk_cents,
            SUM(revenue_cents) as total_revenue
        FROM settled_trades
        WHERE ticker LIKE '%15M%' AND settled_at > ?
    """, (regime_start,)).fetchone()
    n = r["n"]
    if n == 0:
        print("No trades in regime!")
        return
    print(f'Trades: {n}, Wins: {r["wins"]}, Losses: {r["losses"]}, WR: {r["wins"]/n*100:.1f}%')
    print(f'Net PnL: ${r["total_pnl"]/100:.2f}, PnL/trade: ${r["avg_pnl"]/100:.2f}')
    print(f'Total fees: ${r["total_fees"]/100:.2f}, Fees/trade: ${r["total_fees"]/n/100:.2f}')
    print(f'Avg entry: {r["avg_entry"]:.0f}c, Range: {r["min_entry"]}-{r["max_entry"]}c')
    print(f'Avg STC: {r["avg_stc"]:.0f}s, Avg contracts: {r["avg_contracts"]:.1f}')
    print(f'Total risk deployed: ${r["total_risk_cents"]/100:.2f}')

    print("\n--- By Asset ---")
    rows = c.execute("""
        SELECT asset, COUNT(*) as n,
            SUM(CASE WHEN pnl_cents > 0 THEN 1 ELSE 0 END) as wins,
            SUM(pnl_cents) as pnl, SUM(fee_cents) as fees,
            ROUND(AVG(entry_price_cents), 1) as avg_entry,
            ROUND(AVG(seconds_to_close), 1) as avg_stc,
            ROUND(AVG(count), 1) as avg_ctx
        FROM settled_trades WHERE ticker LIKE '%15M%' AND settled_at > ?
        GROUP BY asset ORDER BY pnl DESC
    """, (regime_start,)).fetchall()
    for r in rows:
        L = r["n"] - r["wins"]
        wr = r["wins"] / r["n"] * 100
        print(f'  {r["asset"]}: {r["n"]}t ({r["wins"]}W/{L}L={wr:.0f}%) PnL=${r["pnl"]/100:.2f} '
              f'fees=${r["fees"]/100:.2f} entry={r["avg_entry"]:.0f}c stc={r["avg_stc"]:.0f}s ctx={r["avg_ctx"]:.1f}')

    print("\n--- By Escalation Type ---")
    rows = c.execute("""
        SELECT COALESCE(escalation_type, 'unknown') as esc, COUNT(*) as n,
            SUM(CASE WHEN pnl_cents > 0 THEN 1 ELSE 0 END) as wins,
            SUM(pnl_cents) as pnl, SUM(fee_cents) as fees,
            ROUND(AVG(entry_price_cents), 1) as avg_entry,
            ROUND(AVG(COALESCE(fill_latency_seconds,0)), 1) as avg_fill_lat,
            ROUND(AVG(seconds_to_close), 1) as avg_stc
        FROM settled_trades WHERE ticker LIKE '%15M%' AND settled_at > ?
        GROUP BY esc ORDER BY n DESC
    """, (regime_start,)).fetchall()
    for r in rows:
        L = r["n"] - r["wins"]
        wr = r["wins"] / r["n"] * 100
        print(f'  {r["esc"]}: {r["n"]}t ({r["wins"]}W/{L}L={wr:.0f}%) PnL=${r["pnl"]/100:.2f} '
              f'fees=${r["fees"]/100:.2f} entry={r["avg_entry"]:.0f}c fill_lat={r["avg_fill_lat"]:.1f}s stc={r["avg_stc"]:.0f}s')

    print("\n--- By Entry Price Bucket ---")
    rows = c.execute("""
        SELECT CASE
            WHEN entry_price_cents <= 88 THEN '87-88'
            WHEN entry_price_cents <= 90 THEN '89-90'
            WHEN entry_price_cents <= 92 THEN '91-92'
            WHEN entry_price_cents <= 94 THEN '93-94'
            WHEN entry_price_cents <= 96 THEN '95-96'
            ELSE '97-99' END as bucket,
        COUNT(*) as n, SUM(CASE WHEN pnl_cents > 0 THEN 1 ELSE 0 END) as wins,
        SUM(pnl_cents) as pnl, SUM(fee_cents) as fees, ROUND(AVG(pnl_cents),1) as avg_pnl
        FROM settled_trades WHERE ticker LIKE '%15M%' AND settled_at > ?
        GROUP BY bucket ORDER BY bucket
    """, (regime_start,)).fetchall()
    for r in rows:
        L = r["n"] - r["wins"]
        wr = r["wins"] / r["n"] * 100 if r["n"] > 0 else 0
        print(f'  {r[0]}: {r["n"]}t ({r["wins"]}W/{L}L={wr:.0f}%) PnL=${r["pnl"]/100:.2f} '
              f'fees=${r["fees"]/100:.2f} avg=${r["avg_pnl"]/100:.2f}/trade')

    print("\n--- By STC Bucket ---")
    rows = c.execute("""
        SELECT CASE
            WHEN seconds_to_close < 120 THEN 'a.<120s'
            WHEN seconds_to_close < 180 THEN 'b.120-180s'
            WHEN seconds_to_close < 240 THEN 'c.180-240s'
            WHEN seconds_to_close < 300 THEN 'd.240-300s'
            WHEN seconds_to_close < 600 THEN 'e.300-600s'
            ELSE 'f.600s+' END as bucket,
        COUNT(*) as n, SUM(CASE WHEN pnl_cents > 0 THEN 1 ELSE 0 END) as wins,
        SUM(pnl_cents) as pnl, SUM(fee_cents) as fees,
        ROUND(AVG(entry_price_cents),1) as avg_entry
        FROM settled_trades WHERE ticker LIKE '%15M%' AND settled_at > ?
        GROUP BY bucket ORDER BY bucket
    """, (regime_start,)).fetchall()
    for r in rows:
        L = r["n"] - r["wins"]
        wr = r["wins"] / r["n"] * 100 if r["n"] > 0 else 0
        print(f'  {r[0]}: {r["n"]}t ({r["wins"]}W/{L}L={wr:.0f}%) PnL=${r["pnl"]/100:.2f} '
              f'fees=${r["fees"]/100:.2f} entry={r["avg_entry"]:.0f}c')

    print("\n--- Individual Losses ---")
    rows = c.execute("""
        SELECT ticker, asset, entry_price_cents, count, pnl_cents, fee_cents,
            seconds_to_close, COALESCE(escalation_type,'unknown') as esc,
            calibrated_prob, edge, settled_at
        FROM settled_trades WHERE ticker LIKE '%15M%' AND settled_at > ? AND pnl_cents <= 0
        ORDER BY pnl_cents ASC
    """, (regime_start,)).fetchall()
    if not rows:
        print("  No losses in current regime!")
    else:
        for r in rows:
            prob = r["calibrated_prob"] or 0
            edge = r["edge"] or 0
            print(f'  {r["ticker"]} {r["asset"]} entry={r["entry_price_cents"]}c x{r["count"]} '
                  f'pnl=${r["pnl_cents"]/100:.2f} fee=${r["fee_cents"]/100:.2f} stc={r["seconds_to_close"]:.0f}s '
                  f'esc={r["esc"]} prob={prob:.3f} edge={edge:.4f}')

    print("\n--- Maker vs Taker Breakdown ---")
    rows = c.execute("""
        SELECT
            CASE WHEN maker_price_cents IS NOT NULL AND maker_price_cents > 0 THEN 'maker' ELSE 'taker_or_unknown' END as fill_type,
            COUNT(*) as n,
            SUM(CASE WHEN pnl_cents > 0 THEN 1 ELSE 0 END) as wins,
            SUM(pnl_cents) as pnl, SUM(fee_cents) as fees,
            ROUND(AVG(entry_price_cents),1) as avg_entry
        FROM settled_trades WHERE ticker LIKE '%15M%' AND settled_at > ?
        GROUP BY fill_type
    """, (regime_start,)).fetchall()
    for r in rows:
        L = r["n"] - r["wins"]
        wr = r["wins"] / r["n"] * 100
        print(f'  {r[0]}: {r["n"]}t ({r["wins"]}W/{L}L={wr:.0f}%) PnL=${r["pnl"]/100:.2f} '
              f'fees=${r["fees"]/100:.2f} entry={r["avg_entry"]:.0f}c')

    print("\n--- Fee Analysis ---")
    r = c.execute("""
        SELECT SUM(fee_cents) as total_fees,
            SUM(CASE WHEN fee_cents <= 1 THEN 1 ELSE 0 END) as maker_fee_count,
            SUM(CASE WHEN fee_cents > 1 THEN 1 ELSE 0 END) as taker_fee_count,
            SUM(CASE WHEN fee_cents <= 1 THEN fee_cents ELSE 0 END) as maker_fees,
            SUM(CASE WHEN fee_cents > 1 THEN fee_cents ELSE 0 END) as taker_fees
        FROM settled_trades WHERE ticker LIKE '%15M%' AND settled_at > ?
    """, (regime_start,)).fetchone()
    print(f'  Total fees: ${r["total_fees"]/100:.2f}')
    print(f'  Maker-fee trades (fee<=1c): {r["maker_fee_count"]} totaling ${r["maker_fees"]/100:.2f}')
    print(f'  Taker-fee trades (fee>1c): {r["taker_fee_count"]} totaling ${r["taker_fees"]/100:.2f}')


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 2: Leak Analysis — Evaluated Opportunities
# ═══════════════════════════════════════════════════════════════════════════════
def section_2(c, regime_start):
    print("\n" + "=" * 80)
    print(f"SECTION 2: LEAK ANALYSIS — Evaluated Opportunities (since {regime_start})")
    print("=" * 80)

    # 2a. Filter stage breakdown
    print("\n--- Filter stage breakdown (15M only) ---")
    rows = c.execute("""
        SELECT filter_stage, COUNT(*) as n,
            SUM(CASE WHEN market_result = 'yes' THEN 1 ELSE 0 END) as would_win,
            SUM(CASE WHEN market_result = 'no' THEN 1 ELSE 0 END) as would_lose,
            SUM(CASE WHEN market_result IS NULL OR status = 'pending' THEN 1 ELSE 0 END) as unsettled,
            ROUND(AVG(market_price), 1) as avg_price,
            ROUND(AVG(fee_adjusted_edge), 4) as avg_fae,
            ROUND(AVG(seconds_to_close), 0) as avg_stc
        FROM evaluated_opportunities
        WHERE evaluation_time > ?
          AND (product_type IS NULL OR product_type = '15m')
        GROUP BY filter_stage ORDER BY n DESC
    """, (regime_start,)).fetchall()
    for r in rows:
        settled = r["would_win"] + r["would_lose"]
        wr = r["would_win"] / settled * 100 if settled > 0 else 0
        fae = r["avg_fae"] if r["avg_fae"] else 0
        print(f'  {r["filter_stage"]}: {r["n"]} evals ({r["would_win"]}W/{r["would_lose"]}L/{r["unsettled"]}pending, '
              f'WR={wr:.0f}%) avg_price={r["avg_price"]:.0f}c avg_fae={fae:.4f} avg_stc={r["avg_stc"]:.0f}s')

    # 2b. Counterfactual P&L for rejected opportunities
    print("\n--- Counterfactual P&L for rejected opps (settled, 87-99c) ---")
    rows = c.execute("""
        SELECT filter_stage,
            COUNT(*) as n,
            SUM(CASE WHEN market_result = 'yes' THEN 1 ELSE 0 END) as wins,
            SUM(CASE WHEN market_result = 'no' THEN 1 ELSE 0 END) as losses,
            SUM(CASE WHEN market_result = 'yes' THEN (100 - market_price) ELSE -market_price END) as gross_pnl_cents,
            ROUND(AVG(market_price), 1) as avg_price,
            ROUND(AVG(fee_adjusted_edge), 4) as avg_fae,
            ROUND(AVG(seconds_to_close), 0) as avg_stc
        FROM evaluated_opportunities
        WHERE evaluation_time > ?
          AND (product_type IS NULL OR product_type = '15m')
          AND filter_stage != 'candidate'
          AND market_result IS NOT NULL
          AND market_price BETWEEN 87 AND 99
        GROUP BY filter_stage ORDER BY gross_pnl_cents DESC
    """, (regime_start,)).fetchall()
    for r in rows:
        wr = r["wins"] / (r["wins"] + r["losses"]) * 100 if (r["wins"] + r["losses"]) > 0 else 0
        fae = r["avg_fae"] if r["avg_fae"] else 0
        print(f'  {r["filter_stage"]}: {r["n"]} settled ({r["wins"]}W/{r["losses"]}L={wr:.0f}%) '
              f'gross_PnL=${r["gross_pnl_cents"]/100:.2f} avg_price={r["avg_price"]:.0f}c avg_fae={fae:.4f} avg_stc={r["avg_stc"]:.0f}s')

    # 2c. Insufficient edge near threshold
    print("\n--- Insufficient edge: distribution near threshold ---")
    rows = c.execute("""
        SELECT CASE
            WHEN fee_adjusted_edge >= 0.005 AND fee_adjusted_edge < 0.007 THEN 'a.0.5-0.7%'
            WHEN fee_adjusted_edge >= 0.007 AND fee_adjusted_edge < 0.009 THEN 'b.0.7-0.9%'
            WHEN fee_adjusted_edge >= 0.009 AND fee_adjusted_edge < 0.012 THEN 'c.0.9-1.2%'
            WHEN fee_adjusted_edge >= 0.012 AND fee_adjusted_edge < 0.018 THEN 'd.1.2-1.8%'
            WHEN fee_adjusted_edge >= 0.018 THEN 'e.1.8%+'
            WHEN fee_adjusted_edge >= 0.0 THEN 'f.0-0.5%'
            ELSE 'g.negative' END as bucket,
        COUNT(*) as n,
        SUM(CASE WHEN market_result = 'yes' THEN 1 ELSE 0 END) as wins,
        SUM(CASE WHEN market_result = 'no' THEN 1 ELSE 0 END) as losses,
        ROUND(AVG(market_price), 1) as avg_price,
        ROUND(AVG(seconds_to_close), 0) as avg_stc
        FROM evaluated_opportunities
        WHERE evaluation_time > ?
          AND (product_type IS NULL OR product_type = '15m')
          AND filter_stage = 'insufficient_edge'
          AND market_result IS NOT NULL
          AND market_price BETWEEN 87 AND 99
        GROUP BY bucket ORDER BY bucket
    """, (regime_start,)).fetchall()
    for r in rows:
        total = r["wins"] + r["losses"]
        wr = r["wins"] / total * 100 if total > 0 else 0
        print(f'  {r[0]}: {r["n"]} settled ({r["wins"]}W/{r["losses"]}L={wr:.0f}%) '
              f'avg_price={r["avg_price"]:.0f}c avg_stc={r["avg_stc"]:.0f}s')

    # 2d. Price out of range breakdown
    print("\n--- Price out of range: by price bucket (settled) ---")
    rows = c.execute("""
        SELECT CASE
            WHEN market_price < 80 THEN 'a.<80c'
            WHEN market_price < 85 THEN 'b.80-84c'
            WHEN market_price < 87 THEN 'c.85-86c'
            WHEN market_price >= 99 THEN 'e.99c+'
            ELSE 'd.87-98c' END as bucket,
        COUNT(*) as n,
        SUM(CASE WHEN market_result = 'yes' THEN 1 ELSE 0 END) as wins,
        SUM(CASE WHEN market_result = 'no' THEN 1 ELSE 0 END) as losses,
        SUM(CASE WHEN market_result = 'yes' THEN (100 - market_price) ELSE -market_price END) as gross_pnl
        FROM evaluated_opportunities
        WHERE evaluation_time > ?
          AND (product_type IS NULL OR product_type = '15m')
          AND filter_stage = 'price_out_of_range'
          AND market_result IS NOT NULL
        GROUP BY bucket ORDER BY bucket
    """, (regime_start,)).fetchall()
    for r in rows:
        total = r["wins"] + r["losses"]
        wr = r["wins"] / total * 100 if total > 0 else 0
        print(f'  {r[0]}: {r["n"]} settled ({r["wins"]}W/{r["losses"]}L={wr:.0f}%) gross_PnL=${r["gross_pnl"]/100:.2f}')

    # 2e. Candidate details
    print("\n--- Candidate details (trades that passed all filters) ---")
    rows = c.execute("""
        SELECT ticker, asset, market_price, fee_adjusted_edge, calibrated_prob,
            seconds_to_close, position_size, kelly_f, drawdown_scaler, market_result, evaluation_time
        FROM evaluated_opportunities
        WHERE evaluation_time > ?
          AND (product_type IS NULL OR product_type = '15m')
          AND filter_stage = 'candidate'
          AND market_result IS NOT NULL
        ORDER BY evaluation_time
    """, (regime_start,)).fetchall()
    for r in rows:
        fae = r["fee_adjusted_edge"] if r["fee_adjusted_edge"] else 0
        prob = r["calibrated_prob"] if r["calibrated_prob"] else 0
        ds = r["drawdown_scaler"] if r["drawdown_scaler"] else 1.0
        print(f'  {r["ticker"]} {r["asset"]} price={r["market_price"]}c fae={fae:.4f} prob={prob:.3f} '
              f'stc={r["seconds_to_close"]:.0f}s size={r["position_size"]} kelly={r["kelly_f"]:.4f} '
              f'dd_scaler={ds:.2f} result={r["market_result"]}')

    # 2f. STC distribution of candidates vs rejections
    print("\n--- STC distribution: candidates vs insufficient_edge ---")
    rows = c.execute("""
        SELECT
            CASE
                WHEN seconds_to_close < 120 THEN 'a.<120s'
                WHEN seconds_to_close < 180 THEN 'b.120-180s'
                WHEN seconds_to_close < 240 THEN 'c.180-240s'
                WHEN seconds_to_close < 300 THEN 'd.240-300s'
                WHEN seconds_to_close < 600 THEN 'e.300-600s'
                ELSE 'f.600s+' END as stc_bucket,
            SUM(CASE WHEN filter_stage = 'candidate' THEN 1 ELSE 0 END) as candidates,
            SUM(CASE WHEN filter_stage = 'insufficient_edge' THEN 1 ELSE 0 END) as insuf_edge,
            SUM(CASE WHEN filter_stage = 'price_out_of_range' THEN 1 ELSE 0 END) as price_oor,
            SUM(CASE WHEN filter_stage = 'stc_shadow' THEN 1 ELSE 0 END) as stc_shadow
        FROM evaluated_opportunities
        WHERE evaluation_time > ?
          AND (product_type IS NULL OR product_type = '15m')
        GROUP BY stc_bucket ORDER BY stc_bucket
    """, (regime_start,)).fetchall()
    for r in rows:
        print(f'  {r[0]}: candidates={r["candidates"]} insuf_edge={r["insuf_edge"]} '
              f'price_oor={r["price_oor"]} stc_shadow={r["stc_shadow"]}')

    # 2g. Zero sizing — drawdown-killed trades
    print("\n--- Zero sizing analysis ---")
    rows = c.execute("""
        SELECT COUNT(*) as n,
            SUM(CASE WHEN market_result = 'yes' THEN 1 ELSE 0 END) as wins,
            SUM(CASE WHEN market_result = 'no' THEN 1 ELSE 0 END) as losses,
            ROUND(AVG(drawdown_scaler), 3) as avg_dd,
            ROUND(AVG(fee_adjusted_edge), 4) as avg_fae,
            ROUND(AVG(market_price), 1) as avg_price
        FROM evaluated_opportunities
        WHERE evaluation_time > ?
          AND (product_type IS NULL OR product_type = '15m')
          AND filter_stage = 'zero_sizing'
          AND market_result IS NOT NULL
    """, (regime_start,)).fetchall()
    for r in rows:
        total = (r["wins"] or 0) + (r["losses"] or 0)
        wr = r["wins"] / total * 100 if total > 0 else 0
        print(f'  zero_sizing: {r["n"]} settled ({r["wins"]}W/{r["losses"]}L={wr:.0f}%) '
              f'avg_dd={r["avg_dd"]} avg_fae={r["avg_fae"]} avg_price={r["avg_price"]}c')


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 3: Extended Regime Data for Statistical Power
# ═══════════════════════════════════════════════════════════════════════════════
def section_3(c, narrow_start, medium_start, broad_start):
    print("\n" + "=" * 80)
    print("SECTION 3: EXTENDED REGIME DATA FOR STATISTICAL POWER")
    print("=" * 80)

    for label, start in [("Broad", broad_start), ("Medium", medium_start), ("Narrow", narrow_start)]:
        r = c.execute("""
            SELECT COUNT(*) as n,
                SUM(CASE WHEN pnl_cents > 0 THEN 1 ELSE 0 END) as w,
                SUM(CASE WHEN pnl_cents <= 0 THEN 1 ELSE 0 END) as l,
                SUM(pnl_cents) as pnl
            FROM settled_trades WHERE ticker LIKE '%15M%' AND settled_at > ?
        """, (start,)).fetchone()
        if r["n"]:
            print(f'{label} ({start[:10]}+): {r["n"]} trades ({r["w"]}W/{r["l"]}L={r["w"]/r["n"]*100:.1f}%) PnL=${r["pnl"]/100:.2f}')
        else:
            print(f'{label} ({start[:10]}+): 0 trades')

    # Entry price analysis (broad regime — MIN_ENTRY constant)
    print(f"\n--- MIN_ENTRY_PRICE analysis ({broad_start[:10]}+, MIN_ENTRY=87 throughout) ---")
    rows = c.execute("""
        SELECT entry_price_cents, COUNT(*) as n,
            SUM(CASE WHEN pnl_cents > 0 THEN 1 ELSE 0 END) as w,
            SUM(pnl_cents) as pnl, SUM(fee_cents) as fees,
            ROUND(AVG(count), 1) as avg_ctx
        FROM settled_trades WHERE ticker LIKE '%15M%' AND settled_at > ?
        GROUP BY entry_price_cents ORDER BY entry_price_cents
    """, (broad_start,)).fetchall()
    for r in rows:
        l = r["n"] - r["w"]
        wr = r["w"] / r["n"] * 100
        print(f'  {r["entry_price_cents"]}c: {r["n"]}t ({r["w"]}W/{l}L={wr:.0f}%) '
              f'PnL=${r["pnl"]/100:.2f} fees=${r["fees"]/100:.2f} avg_ctx={r["avg_ctx"]}')

    # Counterfactual: 85-86c price_out_of_range
    print("\n--- Counterfactual: 82-88c price_out_of_range (all data) ---")
    rows = c.execute("""
        SELECT market_price, COUNT(*) as n,
            SUM(CASE WHEN market_result = 'yes' THEN 1 ELSE 0 END) as w,
            SUM(CASE WHEN market_result = 'no' THEN 1 ELSE 0 END) as l,
            SUM(CASE WHEN market_result = 'yes' THEN (100 - market_price) ELSE -market_price END) as gross_pnl
        FROM evaluated_opportunities
        WHERE filter_stage = 'price_out_of_range'
          AND (product_type IS NULL OR product_type = '15m')
          AND market_result IS NOT NULL
          AND market_price BETWEEN 82 AND 88
        GROUP BY market_price ORDER BY market_price
    """).fetchall()
    for r in rows:
        total = r["w"] + r["l"]
        wr = r["w"] / total * 100 if total > 0 else 0
        print(f'  {r["market_price"]}c: {total} settled ({r["w"]}W/{r["l"]}L={wr:.0f}%) gross_PnL=${r["gross_pnl"]/100:.2f}')

    # STC window analysis (broad)
    print(f"\n--- STC analysis ({broad_start[:10]}+ trades) ---")
    rows = c.execute("""
        SELECT CASE
            WHEN seconds_to_close < 90 THEN 'a.<90s'
            WHEN seconds_to_close < 120 THEN 'b.90-120s'
            WHEN seconds_to_close < 180 THEN 'c.120-180s'
            WHEN seconds_to_close < 240 THEN 'd.180-240s'
            WHEN seconds_to_close < 270 THEN 'e.240-270s'
            WHEN seconds_to_close < 300 THEN 'f.270-300s'
            WHEN seconds_to_close < 600 THEN 'g.300-600s'
            ELSE 'h.600s+' END as bucket,
        COUNT(*) as n,
        SUM(CASE WHEN pnl_cents > 0 THEN 1 ELSE 0 END) as w,
        SUM(pnl_cents) as pnl,
        ROUND(AVG(entry_price_cents), 1) as avg_entry,
        ROUND(AVG(fee_cents), 1) as avg_fee
        FROM settled_trades WHERE ticker LIKE '%15M%' AND settled_at > ?
        GROUP BY bucket ORDER BY bucket
    """, (broad_start,)).fetchall()
    for r in rows:
        l = r["n"] - r["w"]
        wr = r["w"] / r["n"] * 100
        print(f'  {r[0]}: {r["n"]}t ({r["w"]}W/{l}L={wr:.0f}%) PnL=${r["pnl"]/100:.2f} '
              f'avg_entry={r["avg_entry"]:.0f}c avg_fee={r["avg_fee"]:.1f}c')

    # Escalation analysis (medium — post fix)
    print(f"\n--- Escalation analysis ({medium_start[:10]}+, post escalation fix) ---")
    rows = c.execute("""
        SELECT COALESCE(escalation_type, 'unknown') as esc,
            COUNT(*) as n,
            SUM(CASE WHEN pnl_cents > 0 THEN 1 ELSE 0 END) as w,
            SUM(pnl_cents) as pnl, SUM(fee_cents) as fees,
            ROUND(AVG(entry_price_cents), 1) as avg_entry,
            ROUND(AVG(COALESCE(fill_latency_seconds,0)), 1) as avg_lat
        FROM settled_trades WHERE ticker LIKE '%15M%' AND settled_at > ?
        GROUP BY esc ORDER BY n DESC
    """, (medium_start,)).fetchall()
    for r in rows:
        l = r["n"] - r["w"]
        wr = r["w"] / r["n"] * 100
        print(f'  {r["esc"]}: {r["n"]}t ({r["w"]}W/{l}L={wr:.0f}%) PnL=${r["pnl"]/100:.2f} '
              f'fees=${r["fees"]/100:.2f} entry={r["avg_entry"]:.0f}c lat={r["avg_lat"]:.1f}s')

    # BTC analysis
    print(f"\n--- BTC analysis: why no BTC trades? ({narrow_start[:10]}+) ---")
    rows = c.execute("""
        SELECT filter_stage, COUNT(*) as n,
            ROUND(AVG(market_price), 1) as avg_price,
            ROUND(AVG(fee_adjusted_edge), 4) as avg_fae,
            ROUND(AVG(seconds_to_close), 0) as avg_stc
        FROM evaluated_opportunities
        WHERE evaluation_time > ?
          AND asset = 'BTC'
          AND (product_type IS NULL OR product_type = '15m')
        GROUP BY filter_stage ORDER BY n DESC
    """, (narrow_start,)).fetchall()
    for r in rows:
        fae = r["avg_fae"] if r["avg_fae"] else 0
        print(f'  {r["filter_stage"]}: {r["n"]} evals avg_price={r["avg_price"]:.0f}c avg_fae={fae:.4f} avg_stc={r["avg_stc"]:.0f}s')

    print(f"\n--- BTC historical performance ({broad_start[:10]}+) ---")
    r = c.execute("""
        SELECT COUNT(*) as n,
            SUM(CASE WHEN pnl_cents > 0 THEN 1 ELSE 0 END) as w,
            SUM(pnl_cents) as pnl, SUM(fee_cents) as fees,
            ROUND(AVG(entry_price_cents), 1) as avg_entry
        FROM settled_trades WHERE ticker LIKE '%15M%' AND asset = 'BTC' AND settled_at > ?
    """, (broad_start,)).fetchone()
    if r["n"]:
        l = r["n"] - r["w"]
        wr = r["w"] / r["n"] * 100
        print(f'  BTC: {r["n"]}t ({r["w"]}W/{l}L={wr:.0f}%) PnL=${r["pnl"]/100:.2f} '
              f'fees=${r["fees"]/100:.2f} entry={r["avg_entry"]:.0f}c')
    else:
        print("  No BTC trades in period")

    # All losses (broad)
    print(f"\n--- All losses ({broad_start[:10]}+) ---")
    rows = c.execute("""
        SELECT ticker, asset, entry_price_cents, count, pnl_cents, fee_cents,
            seconds_to_close, COALESCE(escalation_type,'unknown') as esc, settled_at
        FROM settled_trades WHERE ticker LIKE '%15M%' AND settled_at > ? AND pnl_cents <= 0
        ORDER BY settled_at
    """, (broad_start,)).fetchall()
    for r in rows:
        print(f'  {r["settled_at"][:16]} {r["ticker"]} {r["asset"]} entry={r["entry_price_cents"]}c '
              f'x{r["count"]} pnl=${r["pnl_cents"]/100:.2f} fee=${r["fee_cents"]/100:.2f} '
              f'stc={r["seconds_to_close"]:.0f}s esc={r["esc"]}')

    # Edge distribution of candidates
    print(f"\n--- Edge distribution of candidates ({broad_start[:10]}+) ---")
    rows = c.execute("""
        SELECT CASE
            WHEN fee_adjusted_edge < 0.010 THEN 'a.<1.0%'
            WHEN fee_adjusted_edge < 0.015 THEN 'b.1.0-1.5%'
            WHEN fee_adjusted_edge < 0.020 THEN 'c.1.5-2.0%'
            WHEN fee_adjusted_edge < 0.030 THEN 'd.2.0-3.0%'
            WHEN fee_adjusted_edge < 0.040 THEN 'e.3.0-4.0%'
            ELSE 'f.4.0%+' END as bucket,
        COUNT(*) as n,
        SUM(CASE WHEN market_result = 'yes' THEN 1 ELSE 0 END) as w,
        SUM(CASE WHEN market_result = 'no' THEN 1 ELSE 0 END) as l,
        ROUND(AVG(position_size), 1) as avg_size,
        ROUND(AVG(market_price), 1) as avg_price
        FROM evaluated_opportunities
        WHERE evaluation_time > ?
          AND (product_type IS NULL OR product_type = '15m')
          AND filter_stage = 'candidate'
          AND market_result IS NOT NULL
        GROUP BY bucket ORDER BY bucket
    """, (broad_start,)).fetchall()
    for r in rows:
        total = r["w"] + r["l"]
        wr = r["w"] / total * 100 if total > 0 else 0
        print(f'  {r[0]}: {r["n"]} trades ({r["w"]}W/{r["l"]}L={wr:.0f}%) '
              f'avg_size={r["avg_size"]:.0f} avg_price={r["avg_price"]:.0f}c')

    # Time of day
    print(f"\n--- Time of day analysis ({broad_start[:10]}+, UTC hours) ---")
    rows = c.execute("""
        SELECT CAST(SUBSTR(settled_at, 12, 2) AS INTEGER) as hour,
            COUNT(*) as n,
            SUM(CASE WHEN pnl_cents > 0 THEN 1 ELSE 0 END) as w,
            SUM(pnl_cents) as pnl
        FROM settled_trades WHERE ticker LIKE '%15M%' AND settled_at > ?
        GROUP BY hour ORDER BY hour
    """, (broad_start,)).fetchall()
    for r in rows:
        l = r["n"] - r["w"]
        wr = r["w"] / r["n"] * 100
        print(f'  {r["hour"]:02d}:00 UTC: {r["n"]}t ({r["w"]}W/{l}L={wr:.0f}%) PnL=${r["pnl"]/100:.2f}')


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 4: Additional Analysis
# ═══════════════════════════════════════════════════════════════════════════════
def section_4(c, narrow_start, broad_start):
    print("\n" + "=" * 80)
    print("SECTION 4: ADDITIONAL ANALYSIS")
    print("=" * 80)

    # Z-score rejections
    print(f"\n--- Z-score rejections ({broad_start[:10]}+, 15M only) ---")
    r = c.execute("""
        SELECT COUNT(*) as n,
            SUM(CASE WHEN market_result = 'yes' THEN 1 ELSE 0 END) as w,
            SUM(CASE WHEN market_result = 'no' THEN 1 ELSE 0 END) as l,
            SUM(CASE WHEN market_result IS NULL THEN 1 ELSE 0 END) as unsettled,
            ROUND(AVG(market_price), 1) as avg_price
        FROM rejected_opportunities
        WHERE rejection_reason LIKE '%z_score%'
          AND (product_type IS NULL OR product_type != 'hourly')
          AND rejection_time > ?
    """, (broad_start,)).fetchone()
    w = r["w"] or 0
    l = r["l"] or 0
    total = w + l
    wr = w / total * 100 if total > 0 else 0
    print(f'  Total: {r["n"]} ({w}W/{l}L/{r["unsettled"] or 0}unsettled, WR={wr:.0f}%) avg_price={r["avg_price"]}c')

    # STC 300-600s counterfactual (key for shadow gate analysis)
    print(f"\n--- STC 300-600s counterfactual ({broad_start[:10]}+, 87-99c) ---")
    rows = c.execute("""
        SELECT CASE
            WHEN seconds_to_close < 360 THEN 'a.300-360s'
            WHEN seconds_to_close < 420 THEN 'b.360-420s'
            WHEN seconds_to_close < 480 THEN 'c.420-480s'
            WHEN seconds_to_close < 540 THEN 'd.480-540s'
            ELSE 'e.540-600s' END as bucket,
        COUNT(*) as n,
        SUM(CASE WHEN market_result = 'yes' THEN 1 ELSE 0 END) as w,
        SUM(CASE WHEN market_result = 'no' THEN 1 ELSE 0 END) as l,
        ROUND(AVG(market_price), 1) as avg_price,
        ROUND(AVG(fee_adjusted_edge), 4) as avg_fae
        FROM evaluated_opportunities
        WHERE evaluation_time > ?
          AND (product_type IS NULL OR product_type = '15m')
          AND seconds_to_close BETWEEN 300 AND 600
          AND market_price BETWEEN 87 AND 99
          AND market_result IS NOT NULL
        GROUP BY bucket ORDER BY bucket
    """, (broad_start,)).fetchall()
    for r in rows:
        total = (r["w"] or 0) + (r["l"] or 0)
        wr = r["w"] / total * 100 if total > 0 else 0
        fae = r["avg_fae"] if r["avg_fae"] else 0
        print(f'  {r[0]}: {total} settled ({r["w"]}W/{r["l"]}L={wr:.0f}%) avg_price={r["avg_price"]}c avg_fae={fae:.4f}')

    # STC shadow gate analysis (new — will populate after deployment)
    print(f"\n--- STC shadow gate data (stc_shadow filter_stage) ---")
    r = c.execute("""
        SELECT COUNT(*) as n,
            SUM(CASE WHEN market_result = 'yes' THEN 1 ELSE 0 END) as w,
            SUM(CASE WHEN market_result = 'no' THEN 1 ELSE 0 END) as l,
            SUM(CASE WHEN market_result IS NULL THEN 1 ELSE 0 END) as pending,
            ROUND(AVG(market_price), 1) as avg_price,
            ROUND(AVG(fee_adjusted_edge), 4) as avg_fae,
            ROUND(AVG(seconds_to_close), 0) as avg_stc
        FROM evaluated_opportunities
        WHERE (product_type IS NULL OR product_type = '15m')
          AND filter_stage = 'stc_shadow'
    """).fetchone()
    if r["n"]:
        total = (r["w"] or 0) + (r["l"] or 0)
        wr = r["w"] / total * 100 if total > 0 else 0
        print(f'  stc_shadow: {r["n"]} total ({r["w"]}W/{r["l"]}L/{r["pending"]}pending, '
              f'WR={wr:.0f}%) avg_price={r["avg_price"]}c avg_fae={r["avg_fae"]} avg_stc={r["avg_stc"]}s')
    else:
        print("  No stc_shadow data yet (will populate after deployment)")

    # Maker fill timing
    print(f"\n--- Maker fill timing ({broad_start[:10]}+) ---")
    rows = c.execute("""
        SELECT CASE
            WHEN fill_latency_seconds < 5 THEN 'a.<5s'
            WHEN fill_latency_seconds < 10 THEN 'b.5-10s'
            WHEN fill_latency_seconds < 20 THEN 'c.10-20s'
            WHEN fill_latency_seconds < 30 THEN 'd.20-30s'
            ELSE 'e.30s+' END as bucket,
        COUNT(*) as n,
        SUM(CASE WHEN pnl_cents > 0 THEN 1 ELSE 0 END) as w,
        SUM(pnl_cents) as pnl,
        ROUND(AVG(fee_cents), 1) as avg_fee,
        ROUND(AVG(seconds_to_close), 0) as avg_stc
        FROM settled_trades
        WHERE ticker LIKE '%15M%' AND settled_at > ? AND fill_latency_seconds IS NOT NULL
        GROUP BY bucket ORDER BY bucket
    """, (broad_start,)).fetchall()
    for r in rows:
        l = r["n"] - r["w"]
        wr = r["w"] / r["n"] * 100
        print(f'  {r[0]}: {r["n"]}t ({r["w"]}W/{l}L={wr:.0f}%) PnL=${r["pnl"]/100:.2f} '
              f'avg_fee={r["avg_fee"]:.1f}c avg_stc={r["avg_stc"]:.0f}s')

    # Fee distribution
    print(f"\n--- Fee distribution ({broad_start[:10]}+) ---")
    rows = c.execute("""
        SELECT fee_cents, COUNT(*) as n,
            SUM(CASE WHEN pnl_cents > 0 THEN 1 ELSE 0 END) as w,
            SUM(pnl_cents) as pnl
        FROM settled_trades WHERE ticker LIKE '%15M%' AND settled_at > ?
        GROUP BY fee_cents ORDER BY fee_cents
    """, (broad_start,)).fetchall()
    for r in rows:
        l = r["n"] - r["w"]
        wr = r["w"] / r["n"] * 100
        print(f'  fee={r["fee_cents"]}c: {r["n"]}t ({r["w"]}W/{l}L={wr:.0f}%) PnL=${r["pnl"]/100:.2f}')

    # Asset performance with ROI
    print(f"\n--- Asset performance ({broad_start[:10]}+) ---")
    rows = c.execute("""
        SELECT asset, COUNT(*) as n,
            SUM(CASE WHEN pnl_cents > 0 THEN 1 ELSE 0 END) as w,
            SUM(pnl_cents) as pnl, SUM(fee_cents) as fees,
            ROUND(AVG(entry_price_cents), 1) as avg_entry,
            SUM(count * entry_price_cents) as risk_deployed
        FROM settled_trades WHERE ticker LIKE '%15M%' AND settled_at > ?
        GROUP BY asset ORDER BY pnl DESC
    """, (broad_start,)).fetchall()
    for r in rows:
        l = r["n"] - r["w"]
        wr = r["w"] / r["n"] * 100
        roi = r["pnl"] / r["risk_deployed"] * 100 if r["risk_deployed"] else 0
        print(f'  {r["asset"]}: {r["n"]}t ({r["w"]}W/{l}L={wr:.0f}%) PnL=${r["pnl"]/100:.2f} '
              f'fees=${r["fees"]/100:.2f} entry={r["avg_entry"]:.0f}c risk=${r["risk_deployed"]/100:.0f} ROI={roi:.2f}%')

    # Drawdown scaler on candidates
    print(f"\n--- Drawdown scaler on candidates ({narrow_start[:10]}+) ---")
    rows = c.execute("""
        SELECT drawdown_scaler, COUNT(*) as n,
            ROUND(AVG(position_size), 1) as avg_size,
            ROUND(AVG(kelly_f), 4) as avg_kelly
        FROM evaluated_opportunities
        WHERE evaluation_time > ?
          AND (product_type IS NULL OR product_type = '15m')
          AND filter_stage = 'candidate'
        GROUP BY drawdown_scaler ORDER BY drawdown_scaler DESC
    """, (narrow_start,)).fetchall()
    for r in rows:
        dd = r["drawdown_scaler"] if r["drawdown_scaler"] else 1.0
        print(f'  dd_scaler={dd:.2f}: {r["n"]} candidates avg_size={r["avg_size"]:.0f} avg_kelly={r["avg_kelly"]:.4f}')

    # Candidates vs trades gap
    print(f"\n--- Candidates vs actual trades ({narrow_start[:10]}+) ---")
    cand = c.execute("""
        SELECT COUNT(*) FROM evaluated_opportunities
        WHERE evaluation_time > ? AND (product_type IS NULL OR product_type = '15m')
        AND filter_stage = 'candidate'
    """, (narrow_start,)).fetchone()[0]
    trades = c.execute("""
        SELECT COUNT(*) FROM settled_trades
        WHERE ticker LIKE '%15M%' AND settled_at > ?
    """, (narrow_start,)).fetchone()[0]
    print(f'  Candidates: {cand}, Trades: {trades}, Gap: {cand - trades}')

    # Vol regime
    print(f"\n--- Vol regime analysis ({broad_start[:10]}+) ---")
    rows = c.execute("""
        SELECT COALESCE(vol_regime, 'unknown') as vr, COUNT(*) as n,
            SUM(CASE WHEN pnl_cents > 0 THEN 1 ELSE 0 END) as w,
            SUM(pnl_cents) as pnl
        FROM settled_trades WHERE ticker LIKE '%15M%' AND settled_at > ?
        GROUP BY vr ORDER BY n DESC
    """, (broad_start,)).fetchall()
    for r in rows:
        l = r["n"] - r["w"]
        wr = r["w"] / r["n"] * 100
        print(f'  {r[0]}: {r["n"]}t ({r["w"]}W/{l}L={wr:.0f}%) PnL=${r["pnl"]/100:.2f}')

    # Losses by STC
    print(f"\n--- Losses breakdown by STC ({broad_start[:10]}+) ---")
    rows = c.execute("""
        SELECT ticker, asset, entry_price_cents, seconds_to_close, pnl_cents, fee_cents,
            COALESCE(escalation_type,'unknown') as esc, count
        FROM settled_trades
        WHERE ticker LIKE '%15M%' AND settled_at > ? AND pnl_cents <= 0
        ORDER BY seconds_to_close
    """, (broad_start,)).fetchall()
    for r in rows:
        print(f'  stc={r["seconds_to_close"]:.0f}s {r["asset"]} entry={r["entry_price_cents"]}c '
              f'x{r["count"]} pnl=${r["pnl_cents"]/100:.2f} fee=${r["fee_cents"]/100:.2f} esc={r["esc"]}')

    # PnL attribution
    print(f"\n--- PnL attribution ({broad_start[:10]}+) ---")
    r = c.execute("""
        SELECT SUM(pnl_cents) as net_pnl,
            SUM(fee_cents) as total_fees,
            SUM(CASE WHEN pnl_cents > 0 THEN pnl_cents ELSE 0 END) as win_pnl,
            SUM(CASE WHEN pnl_cents <= 0 THEN pnl_cents ELSE 0 END) as loss_pnl
        FROM settled_trades WHERE ticker LIKE '%15M%' AND settled_at > ?
    """, (broad_start,)).fetchone()
    print(f'  Win PnL: ${r["win_pnl"]/100:.2f}')
    print(f'  Loss PnL: ${r["loss_pnl"]/100:.2f}')
    print(f'  Total fees: ${r["total_fees"]/100:.2f}')
    print(f'  Net PnL: ${r["net_pnl"]/100:.2f}')
    if r["win_pnl"]:
        print(f'  Fees as % of win PnL: {r["total_fees"]/r["win_pnl"]*100:.1f}%')


# ═══════════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════════
def main():
    args = get_args()
    try:
        conn = sqlite3.connect(args.db)
    except Exception as e:
        print(f"ERROR: Cannot open database '{args.db}': {e}", file=sys.stderr)
        sys.exit(1)
    conn.row_factory = sqlite3.Row

    print(f"Database: {args.db}")
    print(f"Regimes: narrow={args.narrow}, medium={args.medium}, broad={args.broad}")
    print()

    if 1 in args.sections:
        section_1(conn, args.narrow)
    if 2 in args.sections:
        section_2(conn, args.narrow)
    if 3 in args.sections:
        section_3(conn, args.narrow, args.medium, args.broad)
    if 4 in args.sections:
        section_4(conn, args.narrow, args.broad)

    conn.close()
    print("\n" + "=" * 80)
    print("Analysis complete.")


if __name__ == "__main__":
    main()
