---
name: no-side
description: "NO-side shadow data report — volume, pricing verification, settlement outcomes, approach comparisons. Use when: \"how is NO side going?\", \"verify NO-side pricing\", \"NO-side shadow status\", \"check NO-side data\""
---

# NO-Side Shadow Status Report

Comprehensive NO-side shadow data report. Shows data volume, pricing verification, settlement outcomes, approach comparisons, and health checks.

## When to use
- When checking if NO-side data is flowing correctly
- When verifying NO-side pricing fix (deployed 2026-03-07T18:43:00)
- When monitoring NO-side shadow signal quality
- When the user asks "how is NO side going" or similar

## Steps

1. **Checkpoint WAL + copy fresh state.db from VPS**:
   ```
   ssh botuser@45.55.181.30 "cd ~/kalshi-bot-repo && python3 -c \"import sqlite3; c=sqlite3.connect('state.db'); c.execute('PRAGMA wal_checkpoint(PASSIVE)'); c.close()\""
   scp botuser@45.55.181.30:~/kalshi-bot-repo/state.db /tmp/state.db
   ```

2. **Run the status script**:
   ```
   python3 scripts/no_side_status.py --db /tmp/state.db 2>&1
   ```

3. **Present the output** — show the full script output, then add:
   - **Health summary**: Is data flowing? Are prices correct? Any warnings?
   - **Key metrics**: How many signals, settled outcomes, sim PnL
   - **Context**: Why NO-side may or may not be generating tradeable signals (depends on market prices relative to NO_SIDE_MIN_ENTRY_PRICE=70)

## Report sections
| # | Section | What it shows |
|---|---------|---------------|
| 1 | Data Volume | Total signals, pre/post-fix counts, settled vs pending |
| 2 | Pricing Verification | Post-fix NO ask prices, pre-fix data marking |
| 3 | Filter Stage Breakdown | Where NO-side evals land in the pipeline |
| 4 | Shadow Approach Results | Live baseline, A1, A2, market-only per asset |
| 5 | YES vs NO Comparison | Side-by-side PnL and win counts |
| 6 | Data Freshness | Latest timestamps, staleness warnings |
| 7 | Settlement Outcomes | Individual settled NO-side signals with PnL |
| 8 | Health Check | Automated issue detection and status |

## IMPORTANT
- Always checkpoint WAL before SCP
- Always use `/tmp/state.db` — never query VPS state.db directly
- NO-side evaluated_opportunities only appear when NO ask >= NO_SIDE_MIN_ENTRY_PRICE (70c)
- NO-side shadow signals (fifteenm_shadow) appear for ALL prices — they track what-if
- Pre-fix data (before 2026-03-07T18:43:00) used wrong pricing and is marked `pricing_version='no_bid_wrong'`
