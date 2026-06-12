---
name: live-small
description: "Monitor the live-small strategies (longshot premium-harvest maker + TWAP-lock endgame taker): fills, PnL vs backtest, rails state, kill-criteria check, vol-honesty soak gate. Use when: \"how are the strategies doing?\", \"live-small status\", \"check longshot\", \"check twaplock\", \"are we printing?\", \"check the soak\", daily evaluation."
---

# Live-Small Strategy Monitor (longshot + twaplock)

Daily/on-demand evaluation of the two live-small strategies. STATUS
2026-06-12: BOTH PAUSED (live 10:51-12:05Z; blended_rv understated tape
vol 1.4-4x → mispriced entries; see kb/decisions/longshot-twap-live-small-plan.md).
Engines remain ENABLED in SHADOW — this skill works identically on shadow
rows (filter_stage longshot_shadow/twaplock_shadow); use it to judge the
post-vol-fix soak before re-arm. Compares realized performance
against the validated backtest expectations and checks every risk rail.

**Backtest expectations (the bars to beat):**
- Longshot (maker, sells 4-15c deep-OTM, T-12..T-3min): **+4.58c/ct**,
  day-bootstrap CI [+2.82, +6.26] (fillable-only, 12d).
- TWAP-lock (taker, buys p_lock>0.99 side, final [10,90]s): **+14.4c/ct**,
  CI [+11.1, +17.7] (n=359/12d; live uses stricter 0.99 + 5s staleness gate,
  so live frequency may run BELOW the backtest 29.9/day — expected).

**Pre-registered kill criterion (per strategy):** at ~300 settled contracts
or 7 days (whichever first), realized per-ct PnL CI excludes the backtest
point estimate from below AND mean < 0 → flip that strategy's
`*_LIVE_OVERRIDE` to False + autopsy. Daily rails handle the fast failures.

## Usage
```
/live-small            # full report
/live-small today      # today (UTC) only
/live-small rails      # just the rails/kill-switch state
/live-small soak       # vol-honesty soak + selectivity-match (re-arm gate)
```

## Steps

1. **Sync the database.** Follow `.claude/skills/references/db-sync.md`.

2. **Core queries** (single sqlite3 session against /tmp/state.db):

   ```sql
   PRAGMA busy_timeout=10000;

   -- A. Settled PnL per strategy (today + cumulative since go-live)
   SELECT strategy,
          substr(settled_at,1,10) AS day,
          COUNT(*) AS n_trades, SUM(count) AS contracts,
          SUM(pnl_cents - COALESCE(fee_cents,0)) AS net_cents,
          ROUND(1.0*SUM(pnl_cents - COALESCE(fee_cents,0))/SUM(count), 2)
            AS net_per_ct,
          SUM(CASE WHEN pnl_cents - COALESCE(fee_cents,0) > 0 THEN 1
              ELSE 0 END) AS wins
   FROM settled_trades
   WHERE strategy IN ('longshot','twaplock')
     AND settled_at >= '2026-06-12'
   GROUP BY strategy, day ORDER BY day DESC, strategy;

   -- B. Open positions (exposure right now)
   SELECT strategy_group, ticker, side, count, avg_price_cents,
          total_cost_cents, status
   FROM positions
   WHERE strategy_group IN ('longshot','twaplock') AND status='open';

   -- C. Order funnel (fill rate; ls- = longshot maker, tw- = twaplock IOC)
   SELECT substr(client_order_id,1,3) AS pfx, status, COUNT(*) AS n,
          COALESCE(SUM(recorded_fill_count),0) AS recorded_fills
   FROM pending_orders
   WHERE (client_order_id LIKE 'ls-%' OR client_order_id LIKE 'tw-%')
     AND created_at >= '2026-06-12'
   GROUP BY pfx, status ORDER BY pfx, status;

   -- D. Per-asset breakdown (where is the PnL coming from)
   SELECT strategy, asset, SUM(count) AS contracts,
          ROUND(1.0*SUM(pnl_cents - COALESCE(fee_cents,0))/SUM(count), 2)
            AS net_per_ct
   FROM settled_trades
   WHERE strategy IN ('longshot','twaplock') AND settled_at >= '2026-06-12'
   GROUP BY strategy, asset ORDER BY strategy, net_per_ct;

   -- E. Eval-row heartbeat (engines alive and evaluating?)
   SELECT filter_stage, COUNT(*) AS n, MAX(evaluation_time) AS latest
   FROM evaluated_opportunities
   WHERE filter_stage IN ('longshot_live','longshot_shadow',
                          'twaplock_live','twaplock_shadow')
     AND evaluation_time > datetime('now','-6 hours')
   GROUP BY filter_stage;
   ```

3. **Rails state** (combined cap math — mirror `bot/strategy_caps.py`):

   ```sql
   -- Combined realized today (cap trips at <= -2000 cents incl. marks)
   SELECT SUM(pnl_cents - COALESCE(fee_cents,0)) AS combined_realized_today
   FROM settled_trades
   WHERE strategy IN ('longshot','twaplock')
     AND substr(settled_at,1,10) = date('now');

   -- Marked open losses (open positions whose sold/held side is losing
   -- count as full loss): sum total_cost_cents of open rows as the
   -- conservative bound
   SELECT strategy_group, SUM(total_cost_cents) AS max_marked_cents
   FROM positions
   WHERE strategy_group IN ('longshot','twaplock') AND status='open'
   GROUP BY strategy_group;
   ```
   Report: combined net vs the **$20.00 daily cap**; consecutive losing
   days vs the **3-day disable**; whether either engine's disable latch
   has fired (journal signatures below).

4. **Journal signatures** (via `mcp__kalshi-vps__get_recent_logs` or ssh
   `journalctl -u kalshi-bot --since '...' | grep -E '<sig>'`):
   - Activity: `LONGSHOT_QUOTE`, `LONGSHOT_FILL`, `TWAPLOCK_ENTRY`,
     `TWAPLOCK_FILL`
   - Rails firing: `LONGSHOT_DAILY_CAP_HIT`, `TWAPLOCK_DAILY_CAP_HIT`,
     `LONGSHOT_CONSEC_DAYS_DISABLE`, `LONGSHOT_SKIP_side_conflict`
   - Signal-integrity (expected to fire routinely on thin assets):
     `TWAPLOCK_SPOT_STALE`
   - Vol-honesty (Bit V.3 — ANY hit during the soak = gate breach):
     `VOL_HONESTY_BREACH` (also expect routine `TAPE_RV_NONE` on thin
     assets / post-restart warmup — that's abstention, not breach)
   - Anomalies (investigate if seen): `LONGSHOT_PLACE_MALFORMED`,
     `TWAPLOCK_FILL_PARSE_MALFORMED`, `LONGSHOT_CANCEL_FILL_MISMATCH`,
     `LONGSHOT_STALE_DROP`, `RECONCILE_IMPORT_LONGSHOT/TWAPLOCK`
     (boot-recovery fired — check the recovered position's cost basis)

5. **Vol-honesty soak + selectivity-match (`/live-small soak`)** — the
   pre-registered re-arm gate (kb/decisions/longshot-twap-live-small-plan.md,
   agreed w/ operator 2026-06-12 BEFORE any soak data was examined):
   minimum ~3 days from V.1-V.3 deploy, decision Monday 2026-06-15 IFF
   ALL THREE pass with nothing marginal — (1) honesty ratio in [0.8, 1.25]
   continuously on every asset + zero monitor breaches; (2) would-be entry
   selectivity matches the 02b/01b backtest conditioning; (3) regime
   coverage (≥1 weekday US session + overnight-thin + weekend). ANY
   marginal asset/breach/coverage gap → soak extends until clean.

   Data source: the Bit V.3 columns `raw_blended_rv` (RAW engine estimate,
   V.1-R1-M2 sourcing) and `tape_rv300` (independent tape RV) on
   `evaluated_opportunities`. NEVER ratio off the `volatility` column —
   on live-small rows it carries `max(blended_rv, rv300)`, so that ratio
   is ≥ 1 by construction and can't detect deflation. NULLs are honest
   abstention (warmup, buffer gap, event-stale spot) — count them
   (coverage), don't impute them.

   ```sql
   -- F. Per-asset/day honesty ratio: median + p10/p90 + pass-bar check.
   --    Set <SOAK_START> to the V.3 deploy timestamp.
   WITH r AS (
     SELECT asset, substr(evaluation_time,1,10) AS day,
            1.0*raw_blended_rv/tape_rv300 AS ratio
     FROM evaluated_opportunities
     WHERE raw_blended_rv IS NOT NULL AND tape_rv300 > 0
       AND evaluation_time >= '<SOAK_START>'
   ),
   ranked AS (
     SELECT asset, day, ratio,
            ROW_NUMBER() OVER (PARTITION BY asset, day ORDER BY ratio) AS rn,
            COUNT(*)    OVER (PARTITION BY asset, day) AS n
     FROM r
   )
   SELECT asset, day, n,
          ROUND(MAX(CASE WHEN rn = (n+1)/2 THEN ratio END), 3)            AS median,
          ROUND(MAX(CASE WHEN rn = CAST(0.10*n AS INT)+1 THEN ratio END), 3) AS p10,
          ROUND(MAX(CASE WHEN rn = CAST(0.90*n AS INT)+1 THEN ratio END), 3) AS p90,
          CASE WHEN MAX(CASE WHEN rn = (n+1)/2 THEN ratio END)
                    BETWEEN 0.8 AND 1.25
               THEN 'PASS' ELSE 'FAIL' END AS gate_0_8_1_25
   FROM ranked GROUP BY asset, day ORDER BY asset, day;

   -- F2. Ratio coverage (honest-NULL accounting): how many eval rows
   --     carry the pair at all, per asset. Low coverage on an asset =
   --     the tape kept abstaining (spot staleness / buffer gaps) —
   --     that's a coverage gap for gate (3), not a pass.
   SELECT asset,
          COUNT(*) AS rows_total,
          SUM(raw_blended_rv IS NOT NULL) AS rows_with_ratio,
          ROUND(100.0*SUM(raw_blended_rv IS NOT NULL)/COUNT(*), 1) AS pct
   FROM evaluated_opportunities
   WHERE evaluation_time >= '<SOAK_START>'
     AND asset IN ('BTC','ETH','SOL','XRP','HYPE','DOGE','BNB')
     AND product_type = '15m'
   GROUP BY asset ORDER BY pct;

   -- G. Selectivity match (gate 2): would-be entries per strategy/day +
   --    condition stats. Engine eval rows emit ONLY when the entry
   --    condition passes, deduped per (ticker, side) per window — so
   --    row counts ≈ distinct would-be quotes/entries.
   SELECT filter_stage, substr(evaluation_time,1,10) AS day,
          COUNT(*) AS would_be_entries,
          COUNT(DISTINCT asset) AS assets,
          ROUND(AVG(market_price),1)      AS avg_ask_c,     -- longshot: SOLD side's executable ask
          MIN(market_price)               AS min_ask_c,
          MAX(market_price)               AS max_ask_c,
          ROUND(AVG(1.0-calibrated_prob),4) AS avg_p_sold,  -- longshot: p of the sold side
          ROUND(AVG(seconds_to_close),0)  AS avg_stc
   FROM evaluated_opportunities
   WHERE filter_stage IN ('longshot_shadow','twaplock_shadow',
                          'longshot_live','twaplock_live')
     AND evaluation_time >= '<SOAK_START>'
   GROUP BY filter_stage, day ORDER BY day, filter_stage;
   ```

   **Pass-bars for the verdict:**
   - **F:** every asset×day `gate_0_8_1_25 = PASS`, p10/p90 not wildly
     outside the band (the gate says "continuously" — a passing median
     hiding a multi-hour excursion is marginal → extend). Cross-check
     zero `VOL_HONESTY_BREACH` journal hits over the same span (step 4).
   - **G longshot:** condition stats must sit inside the validated 02b
     conditioning: ask band 4-15c, STC 180-720s, p_sold ≤ ask/100 × 0.5.
     Volume bar: the 02b corpus averaged ~10-20 qualifying (ticker,side)
     quotes/day-scale (cross-check against
     `scripts/research/genhunt/02b_longshot_fillable_validation.py`
     replay on the same dates if rates look off by >2x either way).
   - **G twaplock:** entries/day vs the backtest 29.9/day — live runs
     BELOW (0.99 threshold + 5s staleness gate vs validated 0.95), so
     expect single digits; ZERO for a full day with markets moving =
     investigate the gate chain (TWAPLOCK_SPOT_STALE rate first).
   - Selectivity FAR ABOVE backtest = the honest-vol rewire didn't bite
     (conditions passing that the backtest's vol would have rejected);
     FAR BELOW = over-abstention (staleness gates eating coverage).
     Either direction is a gate-(2) fail.

6. **Verdict block** — always end with:
   - Per strategy: contracts settled to date / ~300 evaluation target,
     realized net/ct vs backtest bar, on-track | lagging | KILL-CRITERION-MET.
   - Rails: combined day PnL vs -$20, losing-day streak vs 3, latches clear?
   - Soak (while PAUSED): per-asset honesty-gate PASS/FAIL grid, breach
     count, selectivity-match verdict, regime-coverage checklist.
   - Fill-rate sanity (longshot): fills/quotes ratio — collapse vs the
     backtest fillable-share suggests live adverse selection; flag if the
     ratio looks degenerate (≈0% or ≈100%).
   - Frequency sanity (twaplock): entries/day vs backtest 29.9 (live will
     be lower due to 0.99 + staleness gates; ZERO entries for a full day
     with markets moving = investigate the gate chain, not the market).

## Interpretation guardrails

- **Use corrected PnL discipline**: if `phantom_corrections` has rows for
  ls-/tw- tickers, LEFT JOIN it — the local ledger can drift from Kalshi
  truth (memory: feedback_use_corrected_pnl_always).
- Small-n humility: at 2-3ct sizing, daily PnL swings ±$30 are NOISE
  against a +$12-18/day expectation. The kill criterion needs ~300
  contracts; don't celebrate or panic before it.
- Deep-OTM selling is streaky by construction (~92% small wins, ~8%
  ~90c losses). A losing day is expected ~weekly even if the edge is real.
- If `RECONCILE_*` boot-recovery signatures appear after a restart, verify
  recovered positions carry non-zero `avg_price_cents` (the L-1 R7 class).
- Soak ratio sanity: the monitor's [0.6, 1.8] alert band (VOL_HONESTY_*)
  is WIDER than the [0.8, 1.25] soak gate by design — "no breaches" alone
  does NOT imply the soak passes; run query F.
