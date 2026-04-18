---
status: current
updated: 2026-04-18
tags: [dashboard, public, supabase, schema, privacy]
---
# Public Dashboard Schema (v1)

## Why a second snapshot
The operator dashboard (`gabekagan.io/dashboard/`) is a trading terminal — dense, real-time, 155+ keys, designed for one viewer (Gabriel). A public page at `gabekagan.io/performance/` serves a fundamentally different purpose: narrative-led, polished, shareable with investors/peers/friends. It's not "operator with CSS hiding"; it's a different artifact with different data.

Two snapshots, not one. Operator data literally never reaches the public page — even with DevTools, a visitor sees only the sanitized payload because we write a separate Supabase row.

## Architecture
```
dashboard_snapshot.py
  ├── _build_snapshot()        → operator JSONB (155+ keys) → Supabase dashboard_state row id=1
  └── _build_public_snapshot() → public JSONB  (~15 keys)  → Supabase dashboard_state row id=2

gabekagan.io/dashboard/      ← subscribes to id=1 via Supabase Realtime (live)
gabekagan.io/performance/    ← polls id=2 every 5 min (smoother, more credible)
```

Same Supabase table, two rows. Primary key is `id`. Both rows writable with the same service key (bot-side); both readable with the anon key (public-side). Future: if privacy becomes a concern, RLS restricts id=1 to authed users — the architecture is already in the right shape.

## Fields (strict whitelist)
Schema v1. If this article is out of sync with `_build_public_snapshot()`, the code is canonical.

| Field | Type | Meaning |
|---|---|---|
| `schema_version` | int | 1 today. Bump on breaking changes; frontend warns on mismatch. |
| `updated_at` | ISO string | When this row was written by the bot. |
| `since` | `YYYY-MM-DD` | Date of first settled 15M trade (inception). |
| `days_active` | int | Full days from inception to today. |
| `total_trades` | int | 15M product only. |
| `total_wins` | int | |
| `total_losses` | int | |
| `win_rate` | float 0–1 | `wins / total`. |
| `win_rate_ci_lo` | float 0–1 | Wilson 95% CI lower bound. |
| `win_rate_ci_hi` | float 0–1 | Wilson 95% CI upper bound. |
| `cumulative_return_pct` | float | `(sum_pnl_cents) / INITIAL_DEPOSIT_CENTS * 100`. Hides bankroll — shows % only. |
| `sharpe_ratio` | float | Daily-annualized (√365, crypto trades 24/7). From per-day PnL %. |
| `max_drawdown_pct` | float (negative) | True peak-to-trough on equity curve, %. |
| `profit_factor` | float | Gross wins / gross losses. `999.0` sentinel if no losses yet. |
| `daily_return_series` | list | `[{day, cumulative_pct, daily_pct}]` — drives the chart. |
| `calibration_reliability` | list | `[{bucket_lo, bucket_hi, n, predicted, actual}]` — reliability diagram. Only buckets with `n >= 10` included. |

## What's NEVER in the public row
Operator-only data literally cannot leak via this row because the builder only emits the whitelist above. Strict-by-construction.

- `current_balance`, `peak_balance`, `starting_balance` (dollars)
- `active_positions`, `resting_orders`, `active_order`
- Any `*_shadow*` keys (unshipped alpha)
- Per-trade tickers, entry/exit prices
- `raw_prob`, `calibrated_prob`, `edge`, `kelly_f`, model internals
- Error messages, feed health, rate limits
- Calibration model parameters (Beta a/b, temperature T, etc.)

## Update cadence
- Bot writes id=2 every 30s (alongside id=1 — single POST with two rows)
- Frontend polls id=2 every 5 min (no Realtime subscription — hides intraday noise, no OFFLINE flicker, lighter load, more credible as a track record than a livestream)

This is a deliberate design choice: even though the underlying data updates at 30s granularity, the public page deliberately shows smoother/staler data. Investors want the story, not the tick.

## Methodology copy
The page includes a "How it works" section written in Gabriel's voice. Voice profile sourced from:
- `/Users/gabrielkagan/Documents/personal-kb/voice.md` (Register 3: Professional/Analyst)
- `kalshi-bot/whitepaper_investor.md` (content register)

Voice markers preserved:
- "I built" first-person ownership
- "I think" hedge followed by decisive action
- Precise technical terminology (EGARCH, Kelly, calibration)
- Self-awareness concessions ("the hard work isn't the signal — it's the calibration")
- Plain English accessible to non-quant readers

## Disclaimers (required, footer)
- Past performance not indicative of future results
- Not investment advice / not a solicitation
- Personal trading account
- Kalshi contracts are CFTC-regulated event contracts, not securities
- Methodology provided for transparency

## Design choices
- **Dark theme** — matches operator; performs better for financial aesthetic (Bloomberg, TradingView)
- **Cumulative % since inception, not $** — hides bankroll, focuses on return quality
- **Wilson 95% CI on WR** — signals statistical literacy to peers, reassures investors
- **Reliability diagram** — flexes calibration honesty; skeptical readers can verify the model isn't gamed
- **Scroll-only single page** — no navigation, fewer bounces
- **Mobile-first** — people click investor/friend links on phones
- **Open Graph meta tags** — iMessage/Twitter previews cleanly
- **No auth** — operator and public both unauthenticated today; RLS can be added later without frontend changes

## Evolution path
v2 extensions (not implemented):
- Per-asset breakdown (aggregate WR by BTC/ETH/SOL/XRP)
- Rolling 30d / 7d metrics alongside inception-to-date
- Optional: trade-count histogram by day (shows consistency, not quiet periods)
- Optional: auth-gated operator view (Supabase magic link)

Anything added must go through this whitelist. Adding a field is a deliberate disclosure decision, not a side effect.

## Related
- [[concepts/dashboard-architecture.md]] — operator side
- [[decisions/dashboard-overhaul-plan.md]] — Phase P in context
- [[failures/dashboard-drift.md]]
