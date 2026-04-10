---
status: in-progress
updated: 2026-04-10
tags: [research, buy-low, intraday-momentum, shadow, debunked-then-pending]
---
# Buy-Low-Sell-Higher Investigation (Apr 10, 2026)

## Origin
User asked: "is there potential in buying low (e.g. 30c) and selling higher (e.g. 60c) within a single 15M window?" The bot currently only enters at 75c+ floors so this is a fundamentally new strategy class.

## Initial Misread (Apr 10, debunked)
Naive query showed sub-30c price observations had **85.8% YES settlement rate** on 1020 unique tickers. Looked like massive free money — buy YES at 25c, settle at 100c, +75c per contract.

## What was actually happening
**The bot logs both YES-side AND NO-side market evaluations to `evaluated_opportunities`, both with `market_price` populated.** Found by inspecting per-ticker rows at sub-second resolution:
- KXSOL15M-26APR011300-00 at 16:57:45.677: market_price=23, raw_prob=0.823, rejection='best_ask 23¢ outside range' (YES side)
- Same ticker at 16:57:45.876 (~200ms later): market_price=97, raw_prob=0.177, rejection='NO net_edge -0.5660 < min @97c' (NO side)
- raw_probs sum to ~1.0 — they're complementary YES/NO checks of the same market

The "low YES prices" in the broad query were mostly NO asks on markets that were going to settle YES. Trying to act on this would be buying NO at low prices on YES-bound markets — guaranteed loss.

## Proper YES-side filter
After applying `raw_prob > 0.5 AND filter_stage NOT LIKE '%no_side%'`:
- sub-30c YES side: **n=15, WR=6.7%** (Wilson CI [1.2%, 29.8%])
- 14 of those 15 are stale 0c-1c orderbook entries with STC <10s on markets settling NO
- **The "buy YES low and hold to settle" play does not exist.**

## But the buy-low-sell-HIGHER play might
Different question: not "did it settle YES" but "did the price move higher during the hold so we could exit at profit?"

For YES at 40-60c with real depth (n=11 unique tickers):
| Reached | Hit rate | Wilson CI |
|---------|----------|-----------|
| 60c+ | 8/11 (72.7%) | [43.4%, 90.3%] |
| 70c+ | 8/11 (72.7%) | [43.4%, 90.3%] |
| 80c+ | 6/11 (54.5%) | [28.0%, 78.7%] |

Hold times to reach the higher price: 29s-143s. Pattern is real but n=11 is tiny and CI is wide.

Sub-40c YES with real depth: **n=1 unique ticker** — statistically meaningless.

## Critical gap in our data
We log YES ask (`market_price`) but not YES bid in `evaluated_opportunities`. The exit price for selling YES is the BID, not the ask. Without bid data, we can't tell if "market_price reached 60c" means "we could have sold at 60c" or "we could have bought at 60c (but the bid was 55c)."

## Action: yes_bid_cents column added (Apr 10)
- New column `yes_bid_cents INTEGER` in `evaluated_opportunities`
- Populated by main 15M scanner via `OrderExecutor._best_yes_bid(ob_data)`
- Cached in `StateManager._scan_bid_cache` so all `insert_evaluated_opportunity` call sites pick it up automatically (no need to thread through 50+ call sites)
- Analysis script: `scripts/buy_low_analysis.py`

## Validation criteria for going live
- n ≥ 50 entries at chosen entry/exit pair
- Hit rate ≥ 60% with Wilson 95% CI lower bound > 45%
- Average per-trade EV > 5c after fees
- Same signal works on out-of-sample data (split by date)

## Side benefit: bid data unlocks 4 other analyses
The new column also enables:
1. Validate early-exit signal exit prices were achievable
2. Profit-taking analysis (TP at entry+1c for variance reduction)
3. Spread analysis (where is the YES spread widest?)
4. Quote staleness detection (when NBBO source diverges from real bid)

## Open questions
- **Why does the dual YES/NO logging exist?** Not yet investigated. The bot evaluates both sides for shadow purposes but the same `market_price` field is used for both — should ideally use a `side` column (which exists but isn't always populated correctly for shadow paths).
- **How sparse will yes_bid_cents be?** Depends on how often the scanner has orderbook (vs NBBO fallback). PPO research showed bids available 99-100% of the time for held positions; scan-time may differ.

## Related
- [[ppo-research-questions.md]] — bid availability data for held positions
- [[same-ticker-reentry-analysis.md]] — earlier per-ticker analysis methodology
