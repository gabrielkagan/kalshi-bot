# Performance Analysis

Run a comprehensive performance analysis on VPS data, filtered to the CURRENT config regime.

## CRITICAL RULE
**Always filter to current config regime.** Never mix data from old configs with current. Check CLAUDE.md or memory for when the last major config change happened and filter accordingly. Currently: post Feb 27 05:00 UTC.

## Steps

1. **Write analysis script** to /tmp, covering:
   - Overall stats (W/L/WR/PnL) filtered to current regime
   - Win rate by entry price bucket (87-90, 91-94, 95-99)
   - Win rate by STC bucket (0-60s, 60-120s, 120-180s, 180-270s)
   - Win rate by asset
   - Average win $ vs average loss $
   - Near-miss analysis: insufficient_edge entries with fee_adjusted_edge >= 0.005 and their settlement outcomes
   - Pipeline funnel: filter_stage distribution (last 24h, 15M only)

2. **SCP to VPS and run**: `scp /tmp/perf.py botuser@45.55.181.30:~/kalshi-bot-repo/ && ssh botuser@45.55.181.30 "cd ~/kalshi-bot-repo && python3 perf.py && rm perf.py"`

3. **Present results** with:
   - [VERIFIED] tags on numbers checked against raw data
   - Sample sizes next to every percentage
   - Statistical significance notes for small samples (n < 20: "not significant, could be noise")
   - Actionable recommendations only if data supports them (p < 0.10)
   - Never recommend changes without computing breakeven WR at the relevant price point
