---
status: decided
updated: 2026-03-15
tags: [decision, sol, taker, execution]
date: 2026-03-15
---
# Decision: SOL Taker-First Execution

Date: 2026-03 (exact date not recorded)
Status: Decided

## Context
SOL's maker-first execution path was underperforming all other assets. The standard flow (post maker order, wait for fill, escalate to taker if unfilled) was systematically losing money on SOL due to adverse selection and low fill rates.

Key data at time of decision:
- **SOL maker fill rate: 44.7%** (vs BTC ~55%, ETH ~68%)
- **SOL maker WR: 88.1%** — below the 88.7% taker breakeven threshold
- **400 unfilled SOL candidates at 87c+** had 95% hypothetical WR
- **$101/week missed revenue** from unfilled maker orders that would have been winners
- **~3.4 cent average slip** on escalation_wait path (maker fails, then taker at worse price)

The pattern was clear adverse selection: maker orders fill only when the market moves against the bot, while profitable opportunities escape unfilled.

## Options Considered

### 1. Keep maker-first, tune escalation timeout
- Shorter escalation wait (e.g., 5s instead of 15s) to reduce slip
- Problem: still miss 55% of opportunities during even short waits
- Does not address the fundamental adverse selection on fills

### 2. Taker-first for SOL at all STC
- Bypass maker entirely, submit IOC immediately
- Extra taker fee cost: ~$2/week (negligible vs $101/week missed)
- Trades execute at scan-time price with no delay

### 3. Hybrid: taker-first only at low STC
- Only bypass maker when STC < threshold (e.g., 300s)
- Problem: adverse selection applies at all STC ranges for SOL

## Decision
**Option 2: `SOL_TAKER_FIRST = True`** — SOL bypasses maker entirely and submits direct IOC at all STC values.

The math was unambiguous: $2/week extra taker fees vs $101/week in missed opportunity cost. The 95% WR on unfilled orders confirmed the bot was correctly identifying edge but failing to capture it through maker execution.

Implementation details (bot.py ~12073-12230):
- `TAKER_FIRST_ASSETS = {"SOL"}` gates the override
- Fresh ask fetched before IOC submission (stale NBBO protection)
- `IOC_RETRY_OFFSET = 1` cent above ask for fill certainty
- One IOC retry allowed if first attempt unfills (max 2c chase above original)
- Edge re-checked after price refresh and after offset application
- Path C shadow: logs what maker path would have done for counterfactual tracking

## Empty-Book Maker Fallback
One exception to taker-first: when the SOL orderbook is completely empty (depth=0), there is no one to take from. In this case, the bot falls back to maker execution under strict gates:

- `SOL_EMPTY_BOOK_MAKER_MIN_PRICE = 87` cents (later raised to 91 based on adverse selection data)
- `SOL_EMPTY_BOOK_MIN_STC = 60` seconds (too tight for maker rest below this)
- Requires per-asset lock check (taker-first normally skips this)

Rationale: on a truly empty book, adverse selection does not apply — there is no counterparty to adversely select against. Posting a maker bid can attract fills that IOC cannot.

## Consequences

### Positive
- SOL fill rate increased substantially (IOC fills are near-instant)
- Captured the $101/week in previously missed revenue
- Eliminated the 3.4-cent average slip from escalation delays
- Simplified SOL execution — no maker polling, no escalation timeout logic

### Negative: Empty-Book Fallback Adverse Selection
The empty-book maker fallback path developed its own adverse selection problem. SOL `MAKER_PATIENT` fills showed 88.1% WR — below taker breakeven. When the orderbook is empty and someone fills our maker bid, it tends to be because price is moving against us.

This led to `SOL_EMPTY_BOOK_MAKER_MIN_PRICE` being raised from 87 to 91 cents, restricting the fallback to only high-probability contracts where the adverse selection drag is offset by the high base win rate.

See [[failures/sol-maker-adverse-selection.md]] for the full analysis.

### Constants (current)
| Constant | Value | Purpose |
|----------|-------|---------|
| `SOL_TAKER_FIRST` | `True` | Master switch |
| `TAKER_FIRST_ASSETS` | `{"SOL"}` | Set of assets bypassing maker |
| `SOL_EMPTY_BOOK_MAKER_MIN_PRICE` | 87 | Min price for empty-book maker fallback |
| `SOL_EMPTY_BOOK_MIN_STC` | 60.0 | Min STC for empty-book maker fallback |
| `IOC_RETRY_OFFSET` | 1 | Cents above ask for IOC submission |
| `MAX_CONCURRENT_TAKER_PER_ASSET` | 3 | Safety cap on simultaneous taker positions |

## Related
- [[failures/sol-maker-adverse-selection.md]]
- [[concepts/execution-layer.md]]
- [[concepts/sol-dynamics.md]]
- [[decisions/sol-edge-floor.md]]
- See `kb-research/bot/profitability-acceleration.md` for the 7-question profitability deep dive
