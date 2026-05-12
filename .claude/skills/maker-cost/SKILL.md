---
name: maker-cost
description: "Track cost of maker-first execution — fill rates, unfilled opportunity cost, per-asset breakdown, capture rate analysis. Use when: \"how much are we leaving on the table?\", \"maker vs taker\", \"fill rate check\", \"maker opportunity cost\""
---

# Maker Opportunity Cost Report

Tracks the cost of maker-first execution by computing hypothetical taker P&L for every unfilled maker order. Includes shadow taker tracking data (best ask at maker submission time) for realistic counterfactual pricing.

## When to use
- When evaluating whether maker-first strategy is optimal
- When checking fill rates and missed opportunities
- When the user asks "how much are we leaving on the table" or "maker vs taker"
- When checking shadow taker data coverage (need 200+ orders before strategy decisions)

## Steps

1. **Sync the database.** Follow `.claude/skills/references/db-sync.md` to sync the database.

2. **Run the script**:
   ```bash
   python3 scripts/audit/maker_opportunity_cost.py --db /tmp/state.db --since 2026-03-03 2>&1
   ```
   Adjust `--since` to match the current config regime start.

3. **Present the output** and highlight:
   - Fill rate and trend
   - Opportunity cost in dollars and as % of actual PnL
   - Per-asset breakdown (which assets suffer most from unfilled makers)
   - Shadow taker data coverage (% of orders with actual taker_ask_at_submit)
   - Verdict: is maker-first optimal or should we switch to taker-first?

4. **Present summary**:
   ```
   ## Maker Opportunity Cost Report (since Mar 3)

   ### Fill Rate
   - 48 maker orders submitted, 34 filled (70.8% fill rate)
   - 14 unfilled → escalated to taker (9 filled as taker, 5 expired)

   ### Opportunity Cost
   - Unfilled makers that settled WIN: 8 orders, hypothetical PnL +$42.30
   - Fee savings from maker fills: $0 maker vs ~$18.50 if all taker
   - Net opportunity cost: $42.30 - $18.50 = $23.80

   ### Per-Asset
   | Asset | Fill Rate | Unfilled Cost | Verdict |
   |-------|----------:|--------------:|---------|
   | BTC   | 78%       | $8.20         | OK      |
   | ETH   | 65%       | $12.40        | Monitor |
   | SOL   | 60%       | $18.30        | High    |

   ### Verdict
   Maker-first saves ~$18.50/period in fees but misses ~$42.30 in PnL.
   Net cost: $23.80. Consider taker-first for SOL where fill rate is lowest.
   Shadow taker data: 34/48 orders (70.8%) have shadow pricing — need 200+ for robust analysis.
   ```

## Options
- `--since DATE` — filter to recent data (recommended: use current config regime start)
- `--asset ASSET` — filter to one asset (BTC, ETH, SOL, XRP)

## Report sections
| # | Section | What it shows |
|---|---------|---------------|
| 1 | Fill Rate | Orders submitted vs filled vs unfilled |
| 2 | Unfilled Cost | Hypothetical taker PnL, shadow data coverage |
| 3 | Per-Asset | Which assets have worst fill rates / most missed PnL |
| 4 | Capture Rate | Actual PnL vs theoretical max, fee-adjusted all-taker comparison |
| 5 | Individual Orders | Most recent unfilled orders with settlement outcomes (S=shadow, F=fallback) |

## WHY the fee model matters for maker vs taker decisions

The core tradeoff:
- **Maker fills pay $0 fee.** Kalshi charges no fee on maker (passive) fills.
- **Taker fills pay** `ceil(0.07 × contracts × P × (1-P))` where P = price/100. At 90c with 10 contracts, that's `ceil(0.07 × 10 × 0.9 × 0.1)` = `ceil(0.63)` = $1. At 93c, it's ~$0.46 per contract.

This means:
- **High-price trades (93c+):** Taker fee is small relative to edge. Opportunity cost of missing the fill often exceeds the fee savings.
- **Lower-price trades (86-89c):** Taker fee is larger relative to edge. Maker-first saves more.
- **SOL/XRP:** These assets have thinner Kalshi orderbooks → lower maker fill rates → more missed opportunities. The opportunity cost is asset-specific.

**WHY 200+ orders before strategy decisions?** Shadow taker data (`taker_ask_at_submit`) records the actual ask at maker submission time. Without it, opportunity cost uses the eval-time market price (slightly earlier, less accurate). At 200+ orders, the shadow-vs-fallback pricing difference stabilizes and you can trust the net cost numbers. Below 200, the estimate is noisy.

## Shadow taker tracking
- **`taker_ask_at_submit`**: Best YES ask at the moment the maker order was submitted. Stored in `evaluated_opportunities` DB.
- Orders with shadow data use the actual ask price for taker PnL computation. Pre-shadow orders fall back to `market_price` (best ask at evaluation time, slightly earlier).
- Individual orders show `S` (shadow — real taker price) or `F` (fallback — eval-time price).

## Error Handling

| Situation | Action |
|-----------|--------|
| Script not found | Check: `ls scripts/audit/maker*`. The script may have been renamed or not yet created. |
| Script returns 0 orders | No maker orders in the `--since` window. Try widening: `--since 2026-02-25`. If still 0, the bot may not be trading or all trades were taker. |
| `taker_ask_at_submit` is NULL for all rows | Shadow taker tracking was added later. All orders are using fallback pricing. Report: "0% shadow coverage — all using fallback pricing. Numbers are approximate." |
| Fill rate is 100% | Great, but suspicious. Verify the script is counting IOC taker escalations as separate orders, not lumping them with the original maker. |
| Opportunity cost is negative | This means unfilled makers that settled would have LOST money. This is good — maker-first is correctly avoiding bad fills. Report it as a positive finding. |

## Key concepts
- **Capture rate**: actual PnL / (actual + missed). Lower = more money left on table.
- **Fee adjustment**: maker fills pay $0 fee, taker pays ceil(0.07*C*P*(1-P)). All-taker comparison accounts for this.
- **Opportunity cost**: missed PnL minus extra fees that taker-first would incur.
- Data comes from `evaluated_opportunities` (order_outcome='unfilled') joined to `settled_trades` for settlement.
