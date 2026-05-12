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

## Preflight

Follow `.claude/skills/references/preflight.md` substituting `<wrapper>` = `no-side` and `<X>` = `no_side_status`. Verify `/tmp/state.db` exists, `make -n no-side` parses, and `scripts/audit/no_side_status.py` exists (Bit 11.1d).

## Steps

1. **Sync the database.** Follow `.claude/skills/references/db-sync.md` to sync the database.

2. **Run the status script**:
   ```bash
   make no-side 2>&1
   # wraps `python3 scripts/audit/no_side_status.py --db /tmp/state.db` (Bit 11.3)
   ```

3. **Present the output** — show the full script output, then add:
   - **Health summary**: Is data flowing? Are prices correct? Any warnings?
   - **Key metrics**: How many signals, settled outcomes, sim PnL
   - **Context**: Why NO-side may or may not be generating tradeable signals

4. **Present summary**:
   ```
   ## NO-Side Shadow Status

   ### Data Volume
   - 48 total NO-side signals (post-fix: 34, pre-fix: 14)
   - 22 settled, 26 pending
   - Post-fix signals using correct pricing ✓

   ### Settlement Results (post-fix only)
   | Approach | Settled | W/L   | WR%   | Sim PnL |
   |----------|---------|-------|-------|---------|
   | Live     | 12      | 9/3   | 75.0% | +$4.20  |
   | A1       | 12      | 10/2  | 83.3% | +$6.80  |
   | A2       | 12      | 8/4   | 66.7% | -$2.10  |

   ### Health
   - Data flowing: YES (latest signal 2h ago)
   - Pricing: CORRECT (all post-fix signals verified)
   - Volume: LOW — NO ask rarely hits 70c+ (most markets price NO at 5-15c)
   ```

## WHY NO-side pricing was wrong (and why it matters)

**The bug (pre-2026-03-07T18:43:00):** The bot was using `no_bid` (best NO bid) as the entry price for NO-side shadow signals. But you BUY NO contracts at the `no_ask` (best NO ask), not the bid. This is equivalent to using `yes_bid` when computing YES-side entries — it would massively overstate the edge by using a price you can't actually get.

**The fix:** Changed to use `no_ask` = `100 - yes_bid` (since NO ask = complement of YES bid on Kalshi). All pre-fix data is marked `pricing_version='no_bid_wrong'` and should be excluded from any PnL or WR analysis.

**WHY NO_SIDE_MIN_ENTRY_PRICE = 70c:** NO contracts are the mirror of YES contracts. When YES is priced at 93c, NO is priced at ~7c. The bot only considers NO-side when NO ask >= 70c, which means YES ask <= 30c — i.e., the market thinks the event is unlikely. This is a fundamentally different bet (betting against the consensus) and requires high confidence. 70c was chosen as the mirror of the YES-side experience at 70c+, where the model has shown calibration quality.

**WHY NO-side signals are rare:** Most 15M crypto markets price YES at 85-97c (market thinks above/below is very likely). NO ask = 100 - YES ≈ 3-15c. The NO ask only reaches 70c+ when YES is priced at 30c or below, which means the market sees the outcome as a coin flip or worse. This happens rarely — mainly during high-volatility periods when the threshold is close to the spot price.

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

## Error Handling

| Situation | Action |
|-----------|--------|
| Script not found | Check: `ls scripts/audit/no_side*`. |
| 0 NO-side signals | This is normal if NO ask hasn't reached 70c recently. Report: "No NO-side signals — NO ask hasn't reached MIN_ENTRY_PRICE (70c) in the query window. This means YES prices are consistently high (>30c), which is expected in most market conditions." |
| All signals are pre-fix | No post-fix data has accumulated yet. Report the pre-fix data with a warning: "All data is pre-fix (wrong pricing). Wait for post-fix settlements before drawing conclusions." |
| Pricing verification shows wrong prices | The fix may have regressed. Check `no_ask` values in post-fix data. If they look like bids (very low, 3-15c), the bug is back — escalate. |
| Very low volume (< 5 settled post-fix) | Expected for NO-side. Report what exists but add: "n=N is too small for any conclusions. NO-side generates signals rarely — expect slow data accumulation." |

## IMPORTANT
- Use `/tmp/state.db` — never query VPS state.db directly
- **Always filter to post-fix data** when computing WR or PnL. Pre-fix data used wrong pricing and will give misleading results.
- NO-side evaluated_opportunities only appear when NO ask >= NO_SIDE_MIN_ENTRY_PRICE (70c)
- NO-side shadow signals (fifteenm_shadow) appear for ALL prices — they track what-if at any price
- Pre-fix data (before 2026-03-07T18:43:00) is marked `pricing_version='no_bid_wrong'`
