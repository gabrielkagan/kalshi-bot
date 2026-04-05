---
status: active
updated: 2026-04-02
tags: [sizing, balance, api, cache]
---
# Balance Tracking

## Summary

Balance tracking flows from the Kalshi API through a cached accessor (`_get_balance_cached()`), feeds into sizing, HWM tracking, and per-product bankroll allocation. The cache invalidates on fill events. A sanity cap guards against API glitches. The stale balance problem (scan-time vs execution-time) is mitigated by storing `balance_at_scan` in candidates and optionally refreshing at execution.

## _get_balance_cached()

Located at bot.py:11511. Core balance accessor for the entire system.

1. Checks in-memory cache: `(balance_cents, fetch_time)` tuple
2. If cached and within `BALANCE_CACHE_TTL` (10 seconds), returns cached value
3. Otherwise calls Kalshi API `GET /portfolio/balance` via `KalshiClient.get_balance()`
4. Applies `BALANCE_SANITY_CAP` (default $2,500 via env var `BALANCE_SANITY_CAP_CENTS=250000`)
5. Updates cache and returns

If the API call fails, returns the stale cached value (never returns None if a prior read succeeded). The sanity cap prevents catastrophic oversizing from API glitches returning inflated balances.

## Cache Invalidation

On fill detection (`_on_fill()` at bot.py:13876), the cache is explicitly invalidated:

```python
self._ml.scanner._balance_cache = (None, 0.0)
```

This forces the next `_get_balance_cached()` call to hit the API, ensuring position sizing uses a fresh balance that reflects the fill. Same invalidation happens in `_poll_for_fills()` (bot.py:14915).

## Balance Consumers

### 1. Position Sizing (PositionSizer.compute())

`PositionSizer.compute(win_prob, price_cents, balance_cents)` receives balance from scan. The `balance_cents` parameter is the product-level sizing balance:

- **15M**: Full available balance from `_get_balance_cached()`
- **Hourly**: `balance * HOURLY_BANKROLL_FRACTION (0.10)` = 10% of total
- **SPX**: `balance * SPX_HOURLY_BANKROLL_FRACTION (0.15)` = 15%

The sizing formula: `contracts = floor(balance * risk_fraction / price) * drawdown_scaler`.

### 2. HWM Tracking (PositionSizer.record_balance())

Called once per scan cycle from `_tick()` with the FULL portfolio balance (not fractional). This is critical: fractional bankroll values poison the spike rejection history. See [[concepts/drawdown-scaler.md]] for the full HWM system.

### 3. Per-Ticker / Per-Window Caps

In execution, balance feeds into several cap checks:

- `MAX_RISK_PER_TRADE (0.25)`: `max_by_risk = balance * 0.25 / price`
- Per-asset caps: `BTC_MAX_RISK_PER_TRADE (0.12)`, `XRP_MAX_RISK_PER_TRADE (0.12)`
- Per-window risk: `HOURLY_MAX_WINDOW_RISK (0.15)`, `SPX_HOURLY_MAX_WINDOW_RISK (0.15)`
- `ETH_SUB80_POSITION_CAP (50)`: Hard cap for ETH 75-79c zone

## The Stale Balance Problem

Balance is cached at scan time and stored in `candidate["balance_at_scan"]`. By execution time (potentially seconds later), the real balance may differ due to:

- Another fill consuming balance between scan and execution
- Settlement paying out between scan and execution
- Multiple candidates from the same scan tick competing for the same balance

Mitigation: The executor can optionally call `_get_balance_cached()` again at execution time for a fresher read. The `balance_at_scan` value is used as a fallback and for logging/diagnostics.

## Capital Allocator

`CapitalAllocator` (capital_allocator.py:40) provides `get_budget_cents(strategy, total_balance, locked_by_strategy)`. It previously applied a regime-based cap ($400 ceiling) that was discovered to be permanently throttling all trades -- see [[failures/regime-cap-discovery.md]]. The regime cap has been removed ([[decisions/regime-cap-removal.md]]), but the allocator's observation-mode gating remains active (prevents observation-only strategies from executing).

When available, scan uses the allocator for sizing balance:
```python
_sizing_balance = self._ml.capital_allocator.get_budget_cents(strategy, total_balance, locked)
```

## Key Constants

| Constant | Value | Purpose |
|---|---|---|
| BALANCE_CACHE_TTL | 10.0s | Cache duration for API balance |
| BALANCE_SANITY_CAP | $2,500 | Env var guard against API glitches |
| MAX_RISK_PER_TRADE | 0.25 | Max fraction of balance per trade |
| HOURLY_BANKROLL_FRACTION | 0.10 | Hourly sizes off 10% of total |
| SPX_HOURLY_BANKROLL_FRACTION | 0.15 | SPX sizes off 15% of total |

## Failure Modes

- **Phantom inflation**: API returns balance including pending order exposure. Addressed by HWM warmup (median of 5 readings).
- **Stale cache + rapid fills**: Two fills in <10s, second sizes on pre-first-fill balance. Addressed by cache invalidation on fill.
- **Fractional bankroll in HWM**: Hourly/SPX 10-15% balance recorded to HWM, permanently depresses history. Addressed by caller discipline (only `_tick()` calls `record_balance()` with full portfolio).

## Related

- [[concepts/drawdown-scaler.md]] - HWM system consuming balance readings
- [[failures/regime-cap-discovery.md]] - Capital allocator $400 cap bug
- [[failures/hwm-bugs.md]] - Fractional bankroll poisoning history
