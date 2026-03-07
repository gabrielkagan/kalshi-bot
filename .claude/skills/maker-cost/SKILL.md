# Maker Opportunity Cost Report

Tracks the cost of maker-first execution by computing hypothetical taker P&L for every unfilled maker order. Includes shadow taker tracking data (best ask at maker submission time) for realistic counterfactual pricing.

## When to use
- When evaluating whether maker-first strategy is optimal
- When checking fill rates and missed opportunities
- When the user asks "how much are we leaving on the table" or "maker vs taker"
- When checking shadow taker data coverage (need 200+ orders before strategy decisions)

## Steps

1. **Checkpoint WAL + copy fresh state.db from VPS**:
   ```
   ssh botuser@45.55.181.30 "cd ~/kalshi-bot-repo && python3 -c \"import sqlite3; c=sqlite3.connect('state.db'); c.execute('PRAGMA wal_checkpoint(PASSIVE)'); c.close()\""
   scp botuser@45.55.181.30:~/kalshi-bot-repo/state.db /tmp/state.db
   ```

2. **Run the script**:
   ```
   python3 scripts/maker_opportunity_cost.py --db /tmp/state.db --since 2026-03-03 2>&1
   ```

3. **Present the output** and highlight:
   - Fill rate and trend
   - Opportunity cost in dollars and as % of actual PnL
   - Per-asset breakdown (which assets suffer most from unfilled makers)
   - Shadow taker data coverage (% of orders with actual taker_ask_at_submit)
   - Verdict: is maker-first optimal or should we switch to taker-first?

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

## Shadow taker tracking
- **`taker_ask_at_submit`**: Best YES ask at the moment the maker order was submitted. Stored in `evaluated_opportunities` DB.
- Orders with shadow data use the actual ask price for taker PnL computation. Pre-shadow orders fall back to `market_price` (best ask at evaluation time, slightly earlier).
- **Goal**: Collect 200+ orders with shadow data before making any maker/taker strategy decisions.
- Individual orders show `S` (shadow — real taker price) or `F` (fallback — eval-time price).

## Key concepts
- **Capture rate**: actual PnL / (actual + missed). Lower = more money left on table.
- **Fee adjustment**: maker fills pay $0 fee, taker pays ceil(0.07*C*P*(1-P)). All-taker comparison accounts for this.
- **Opportunity cost**: missed PnL minus extra fees that taker-first would incur.
- Data comes from `evaluated_opportunities` (order_outcome='unfilled') joined to `settled_trades` for settlement.
