# Investigate Loss or Anomaly

When the user reports a loss, anomaly, or unexpected behavior — IMMEDIATELY investigate. Do NOT dismiss, speculate, or explain away. Pull actual data first.

## CRITICAL RULES
- **Never dismiss a loss as "normal variance" without checking the data**
- **Never speculate about causes** — look at the actual trade record
- **Present raw facts first, analysis second**
- **If the user says something is wrong, believe them and investigate**

## Steps

1. **Identify the trade(s)** — from user's description, determine:
   - Ticker/asset (e.g., "SOL loss", "that XRP trade")
   - Approximate time (today, last hour, specific time)
   - If unclear, ask ONE clarifying question

2. **Pull trade data from VPS** — write a script to /tmp, SCP and run:
   ```python
   import sqlite3
   conn = sqlite3.connect('state.db')
   conn.execute('PRAGMA busy_timeout=10000')
   conn.row_factory = sqlite3.Row

   # Find the specific trade(s)
   trades = conn.execute("""
       SELECT ticker, asset, side, count, entry_price_cents, revenue_cents,
              fee_cents, pnl_cents, market_result, settled_at, strategy,
              seconds_to_close, fill_latency_seconds, vol_regime,
              calibrated_prob, edge, kelly_f
       FROM settled_trades
       WHERE [FILTER based on user description]
       ORDER BY settled_at DESC LIMIT 10
   """).fetchall()

   # Also find the corresponding evaluation
   for t in trades:
       evals = conn.execute("""
           SELECT ticker, market_price, calibrated_prob, fee_adjusted_edge,
                  seconds_to_close, position_size, filter_stage,
                  hourly_pre_temp_prob, hourly_applied_temp_t,
                  shadow_cal_prob, evaluation_time, product_type
           FROM evaluated_opportunities
           WHERE ticker = ? ORDER BY evaluation_time DESC LIMIT 3
       """, (t['ticker'],)).fetchall()
   ```

3. **Reconstruct the trade context** — for each trade, report:
   - Entry: price, size, side, STC at entry, fill latency
   - Model: calibrated_prob, edge, kelly_f, vol_regime
   - Execution: strategy, maker/taker, any escalation
   - Outcome: market_result, PnL, revenue, fees
   - Timeline: evaluation_time → entry → settlement

4. **Check for patterns** — query recent similar trades:
   - Same asset, last 24h: W/L ratio
   - Same price bucket: W/L ratio
   - Same STC bucket: W/L ratio
   - Was this a known weak zone? (e.g., XRP at high STC, SOL late in day)

5. **Check if any filter SHOULD have caught this**:
   - Was edge below threshold? What was fee_adjusted_edge?
   - Was STC in a known bad range?
   - Was drawdown scaler active?
   - Was calibrated_prob suspiciously high?

6. **Present findings** — structured as:
   - **The Trade**: what happened (raw facts)
   - **The Context**: what the model saw at entry time
   - **The Pattern**: is this a one-off or part of a trend?
   - **Root Cause**: why did the model get it wrong? (with data)
   - **Action Items**: specific changes to prevent recurrence (only if data supports them)
