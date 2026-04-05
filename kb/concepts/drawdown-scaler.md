---
status: active
updated: 2026-04-03
tags: [sizing, hwm, drawdown, risk]
---
# Drawdown Scaler

## Summary

The drawdown scaler in `PositionSizer` (models.py:994) reduces position sizes when the portfolio balance drops below its rolling 7-day high-water mark (HWM). It is the most bug-prone component in the system, with 5 distinct failure modes documented in [[failures/hwm-bugs.md]]. The scaler operates on a tiered schedule and includes multiple guards against bad balance readings from the Kalshi API.

## Architecture

The scaler lives in `models.py` class `PositionSizer`. Key data structure:

- `_balance_history`: `deque(maxlen=60480)` storing `(timestamp, balance_cents)` tuples. 60480 entries = 7 days at 10-second intervals (one per scan tick).
- `get_rolling_hwm()`: Returns `max(balance for entries within last 7 days)`. Falls back to `starting_balance_cents` if empty, or most recent entry if all entries are older than 7 days.
- `OVERRIDE_HWM` env var: Manual override in dollars, checked at init. Used to unstick phantom HWM inflation.

## Threshold Tiers

The ratio is `portfolio_balance / hwm`. Tiers (from config.py):

| Ratio vs HWM | Scaler | Effect |
|---|---|---|
| >= 85% | 1.0 | Full sizing |
| 75-85% | 0.5 | Half sizing |
| 65-75% | 0.25 | Quarter sizing |
| < 65% | 0.10 | Floor (was 0.0 halt, changed to prevent permanent lockout) |

Contracts are computed as `floor(raw_contracts * scaler)`.

## The Warmup System

On restart, HWM is unknown (in-memory only, lost on restart). The warmup protocol:

1. First 5 balance readings (`_HWM_WARMUP_COUNT`) are collected in `_hwm_warmup_readings`.
2. After 5 readings, the **median** is used to initialize the first `_balance_history` entry.
3. During warmup, `_drawdown_scaler()` returns 1.0 (no scaling).

Median initialization prevents a single inflated API reading from setting a phantom HWM that triggers false drawdown for the entire session. The Kalshi balance API sometimes returns inflated values due to pending order exposure.

## Spike Rejection Guard

`record_balance()` rejects readings that are >20% above the last recorded value (models.py:1161). This prevents phantom HWM inflation from API glitches.

**HWM proximity bypass** (added Mar 30, 2026): If the spike reading is within 110% of the current HWM, the rejection is bypassed. This handles the recovery scenario: balance drops due to settlement timing, then recovers to a known-good level, but the recovery gets rejected as a "spike" relative to the depressed last reading. Without the bypass, the history gets stuck at the depressed value permanently.

**Floor guard**: Readings below 50% of current HWM are also rejected (possible fractional bankroll leak or bad API read).

## Cash-Only Tracking Decision

The ratio uses `_balance_history[-1]` (the last **recorded** portfolio balance), NOT the `balance_cents` parameter passed to `_drawdown_scaler()`. This is critical because:

- `balance_cents` may be a product-level fractional bankroll (hourly's 10%, SPX's 15%)
- `balance_cents` may be available cash only (excluding open position margin)
- Using the parameter directly caused SOL to size at 5 contracts instead of 160 (Mar 27, 2026: available cash $400 vs HWM $1,117 = ratio 0.358 = halt floor)

`record_balance()` must only be called with FULL portfolio balance (cash + open position exposure), once per scan cycle from `_tick()`.

## Dashboard Read-Only Variant

`_drawdown_scaler_readonly()` (models.py:1245) is used by the dashboard snapshot. Key difference: it returns **0.0** at the halt threshold (honest reporting) while the live `_drawdown_scaler()` returns **0.10** (the floor that prevents permanent lockout).

## Key Invariants

- HWM is in-memory only. Every restart loses it. Warmup re-establishes it.
- `record_balance()` is called from `_tick()`, NOT from `compute()` or `_drawdown_scaler()`.
- The scaler is applied AFTER edge-tier risk fraction selection, BEFORE MAX_RISK_PER_TRADE cap.
- Consecutive spike rejections are tracked for alerting but do not change behavior.

## Related

- [[concepts/balance-tracking.md]] - How balance flows into the scaler
- [[failures/hwm-bugs.md]] - Five variants of HWM/drawdown scaler bugs (Mar 25-30, 2026)
