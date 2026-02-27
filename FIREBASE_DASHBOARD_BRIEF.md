# Firebase Dashboard Technical Brief

## Overview

A crypto prediction market trading bot pushes a JSON snapshot to Firebase Realtime Database every 10 seconds. The dashboard should read from this single endpoint and render a real-time monitoring UI.

**Firebase endpoint:** `{FIREBASE_DB_URL}/bot_status.json`
**Update frequency:** Every 10 seconds (HTTP PUT, full replacement)
**Key sanitization:** Firebase-incompatible chars in keys are replaced: `.`→`_`, `$`→`_`, `#`→`_`, `[`→`(`, `]`→`)`, `/`→`|`

---

## Complete Snapshot Schema

Below is every field in the JSON snapshot, organized by dashboard section.

---

### 1. System Status

| Field | Type | Example | Description |
|-------|------|---------|-------------|
| `timestamp` | string (ISO8601) | `"2026-02-22T17:52:14.464656Z"` | When this snapshot was pushed |
| `uptime_seconds` | float | `1417.3` | Seconds since bot process started |
| `bot_status` | string | `"SCANNING"` | One of: `"ERROR"`, `"TRADING"`, `"SCANNING"`, `"IDLE"`, `"UNKNOWN"` |
| `last_error_message` | string | `"'NoneType' object is not iterable"` | Most recent error (empty string if no error in last 120s) |

---

### 2. Account & Balance

| Field | Type | Example | Description |
|-------|------|---------|-------------|
| `current_balance` | float (dollars) | `53.36` | Current account balance |
| `peak_balance` | float (dollars) | `53.36` | Highest balance this session |
| `starting_balance` | float (dollars) | `53.36` | Balance when bot started |
| `drawdown_kelly_mult` | float (0-1) | `1.0` | Kelly sizing multiplier from drawdown protection. 1.0=full, 0.5=halved, 0.25=quartered |

---

### 3. P&L & Win/Loss

| Field | Type | Example | Description |
|-------|------|---------|-------------|
| `win_count` | int | `0` | Total winning trades (all-time settled) |
| `loss_count` | int | `0` | Total losing trades |
| `win_rate` | float (0-1) | `0.0` | `win_count / (win_count + loss_count)` |
| `daily_pnl_cents` | int | `0` | Today's P&L in cents (resets at midnight UTC) |
| `daily_pnl_pct` | float | `0.0` | Today's P&L as percentage of starting balance |
| `consecutive_losses` | int | `0` | Current losing streak (0 if last trade was a win) |

---

### 4. Spot Prices

| Field | Type | Example |
|-------|------|---------|
| `spot_prices` | object | `{"BTC": 67279.42, "ETH": 1936.97, "SOL": 82.87, "XRP": 1.3856}` |

Values are floats (USD) or `null` if no price available. Keys: `"BTC"`, `"ETH"`, `"SOL"`, `"XRP"`.

---

### 5. Volatility Engine

| Field | Type |
|-------|------|
| `current_volatility` | object keyed by asset (`"BTC"`, `"ETH"`, `"SOL"`, `"XRP"`) |

Each asset value is `null` (no data yet) or an object:

| Sub-field | Type | Example | Description |
|-----------|------|---------|-------------|
| `blended_rv` | float | `0.000177` | Blended realized vol (per-5-second scale). Primary vol estimate |
| `regime` | string | `"normal"` | `"normal"` or `"elevated"` (jump-driven) |
| `dvol_5s` | float or null | `0.000208` | Deribit implied vol (BTC/ETH only, null for SOL/XRP) |
| `iv_rv_blend_method` | string | `"stress_override"` | How IV+RV are blended: `"rv_only"`, `"inverse_variance"`, `"stress_override"` |
| `iv_rv_spread` | float or null | `0.975` | `(IV - RV) / RV`. Positive = IV > RV. Null if no IV |
| `rv_1min` | float | `0.000125` | Realized vol (1-min window) |
| `rv_5min` | float | `0.0000657` | Realized vol (5-min window) |
| `rv_15min` | float | `0.0000978` | Realized vol (15-min window) |
| `bv_5min` | float | `0.000124` | Bipower variation (5-min, jump-robust) |
| `bv_15min` | float | `0.000137` | Bipower variation (15-min) |
| `jump_component` | float | `0.0` | Jump variance. >0 during jumps |
| `jump_seconds_remaining` | float | `0` | Seconds until elevated regime expires |
| `num_returns` | int | `180` | Returns accumulated (max 180 = fully warmed) |

**Live example (SOL in elevated regime):**
```json
{
  "blended_rv": 0.000313,
  "regime": "elevated",
  "jump_component": 0.0000759,
  "jump_seconds_remaining": 18.0,
  "dvol_5s": 0.000153,
  "iv_rv_blend_method": "inverse_variance",
  "iv_rv_spread": -0.096,
  "rv_1min": 0.000185, "rv_5min": 0.000153, "rv_15min": 0.000155,
  "bv_5min": 0.000121, "bv_15min": 0.000165,
  "num_returns": 180
}
```

---

### 6. Cross-Exchange Data

| Field | Type |
|-------|------|
| `cross_exchange` | object keyed by asset |

Each asset contains:

```json
{
  "BTC": {
    "prices": {
      "bybit": 67297.9,
      "kraken": 67281.6
    },
    "lead_lag": {
      "consensus_direction": "none",
      "exchanges_above": 0,
      "exchanges_below": 0,
      "max_premium_pct": 0.0,
      "max_discount_pct": 0.0,
      "exchange_premia": {
        "bybit": 0.000318,
        "kraken": 0.0000816
      }
    }
  }
}
```

| Sub-field | Type | Description |
|-----------|------|-------------|
| `prices` | object | Latest price per exchange. Keys: `"bybit"`, `"kraken"` (Binance unavailable due to geo-block). Values: float or null |
| `lead_lag.consensus_direction` | string | `"above"`, `"below"`, `"mixed"`, or `"none"` |
| `lead_lag.exchanges_above` | int | Exchanges trading at premium to Coinbase |
| `lead_lag.exchanges_below` | int | Exchanges trading at discount |
| `lead_lag.max_premium_pct` | float | Highest premium as decimal (0.001 = 0.1%) |
| `lead_lag.max_discount_pct` | float | Highest discount as decimal |
| `lead_lag.exchange_premia` | object | Average premium per exchange (positive = above Coinbase) |

| Field | Type | Example | Description |
|-------|------|---------|-------------|
| `feed_health` | object | `{"binance": false, "bybit": true, "kraken": true}` | WebSocket connection status per exchange |

---

### 7. Order Flow Signals

| Field | Type |
|-------|------|
| `order_flow` | object keyed by asset |

```json
{
  "BTC": {
    "prob_adjustment": 0.015,
    "confidence": "none",
    "consensus": "none",
    "funding_level": "unknown"
  }
}
```

| Sub-field | Type | Values | Description |
|-----------|------|--------|-------------|
| `prob_adjustment` | float | -0.05 to +0.05 | Adjustment applied to calibrated probability |
| `confidence` | string | `"high"`, `"moderate"`, `"low"`, `"none"` | Signal strength |
| `consensus` | string | `"above"`, `"below"`, `"mixed"`, `"none"` | Cross-exchange direction |
| `funding_level` | string | `"extreme"`, `"elevated"`, `"normal"`, `"unknown"` | Perp funding rate level |

---

### 8. Funding Rates

| Field | Type | Example |
|-------|------|---------|
| `funding_rates` | object | `{"BTC": 0.00015, "ETH": null, "SOL": null, "XRP": null}` |

Values are float (decimal rate per 8-hour period, e.g. 0.00015 = 0.015%) or `null` if unavailable.

---

### 9. Active Windows & Timing

| Field | Type | Example | Description |
|-------|------|---------|-------------|
| `seconds_to_next_close` | float | `466.0` | Seconds until nearest market close. `-1` if no active windows |
| `active_windows` | object | `{"total": 4, "by_asset": {"BTC": 1, "ETH": 1, "SOL": 1, "XRP": 1}}` | Count of 15-min market windows currently active |

---

### 10. Rate Limits

| Field | Type | Example |
|-------|------|---------|
| `rate_limits` | object | `{"reads_last_second": 1, "writes_last_second": 0, "read_limit": 30, "write_limit": 30}` |

All integers. Limits are from the Advanced API tier (30/30).

---

### 11. Filter Funnel

| Field | Type |
|-------|------|
| `filter_funnel` | object keyed by asset |

Shows how many markets were rejected at each stage during the most recent scan tick:

```json
{
  "BTC": {
    "evaluated": 1,
    "low_prob": 1,
    "no_orderbook": 0,
    "no_best_ask": 0,
    "price_out_of_range": 0,
    "insufficient_edge": 0,
    "zero_sizing": 0,
    "strategy_wait": 0,
    "candidates": 0
  }
}
```

**Filter pipeline order:** `evaluated` → `low_prob` → `no_orderbook` → `no_best_ask` → `price_out_of_range` → `insufficient_edge` → `zero_sizing` → `strategy_wait` → `candidates`

---

### 12. Active Order

| Field | Type |
|-------|------|
| `active_order` | object or `null` |

Non-null when bot has an order in the market:

```json
{
  "ticker": "KXBTC15M-26FEB221545-45",
  "asset": "BTC",
  "price_cents": 89,
  "count": 3,
  "is_taker": false,
  "is_panic": false,
  "elapsed_seconds": 12.5,
  "seconds_to_close": 87.5,
  "edge": 0.035,
  "kelly_fraction": 0.045,
  "strategy": "MAKER_PATIENT",
  "cal_prob": 0.925
}
```

---

### 13. Positions & Orders

| Field | Type | Description |
|-------|------|-------------|
| `active_positions` | array of objects | Currently held positions |
| `resting_orders` | array of objects | Limit orders waiting to fill |

**Position object:**
```json
{
  "ticker": "KXBTC15M-26FEB221545-45",
  "event_ticker": "KXBTC15M-26FEB221545",
  "asset": "BTC",
  "side": "yes",
  "count": 3,
  "avg_price_cents": 89,
  "total_cost_cents": 267,
  "opened_at": "2026-02-22T15:40:12Z",
  "updated_at": "2026-02-22T15:40:12Z",
  "status": "open"
}
```

**Resting order object:**
```json
{
  "order_id": "abc-123",
  "client_order_id": "cli-456",
  "ticker": "KXBTC15M-26FEB221545-45",
  "event_ticker": "KXBTC15M-26FEB221545",
  "asset": "BTC",
  "side": "yes",
  "action": "buy",
  "count": 3,
  "price_cents": 88,
  "status": "resting",
  "created_at": "2026-02-22T15:40:12Z",
  "updated_at": "2026-02-22T15:40:12Z"
}
```

---

### 14. Recent Trades (Settled)

| Field | Type |
|-------|------|
| `recent_trades` | array of objects (max 10, newest first) |

```json
{
  "ticker": "KXBTC15M-26FEB221545-45",
  "event_ticker": "KXBTC15M-26FEB221545",
  "asset": "BTC",
  "market_result": "yes",
  "side": "yes",
  "count": 3,
  "entry_price_cents": 89,
  "revenue_cents": 300,
  "fee_cents": 2,
  "pnl_cents": 31,
  "settled_at": "2026-02-22T15:45:00Z"
}
```

`market_result`: `"yes"`, `"no"`, `"all_yes"`, `"all_no"`

---

### 15. Simulated Performance (Observation Mode Only)

These fields are only populated when the bot is in observation mode (`OBSERVATION_MODE = True`). In live mode, real trade data appears in `recent_trades` instead.

| Field | Type |
|-------|------|
| `simulated_performance` | object |

```json
{
  "simulated_trades_count": 5,
  "simulated_wins": 0,
  "simulated_losses": 5,
  "simulated_pnl_cents": -430,
  "simulated_win_rate": 0.0,
  "pending_settlement": 0,
  "avg_edge": 0.040927,
  "avg_kelly_f": 0.055227,
  "avg_position_size": 1.0,
  "avg_expected_value": -1.75,
  "pnl_by_strategy": {
    "MAKER_PATIENT": {"count": 5, "pnl_cents": -430, "wins": 0}
  },
  "pnl_by_vol_regime": {
    "normal": {"count": 5, "pnl_cents": -430, "wins": 0}
  },
  "pnl_by_asset": {
    "XRP": {"count": 5, "pnl_cents": -430, "wins": 0}
  }
}
```

| Sub-field | Type | Description |
|-----------|------|-------------|
| `simulated_trades_count` | int | Total observation trades logged |
| `simulated_wins` | int | Trades where counterfactual_pnl > 0 |
| `simulated_losses` | int | Trades where counterfactual_pnl <= 0 |
| `simulated_pnl_cents` | int | Sum of all counterfactual P&L |
| `simulated_win_rate` | float (0-1) | wins / (wins + losses) |
| `pending_settlement` | int | Trades awaiting market resolution |
| `avg_edge` | float | Mean edge across observation trades |
| `avg_kelly_f` | float | Mean Kelly fraction |
| `avg_position_size` | float | Mean contracts per trade |
| `avg_expected_value` | float or null | Mean EV in cents (new field) |
| `pnl_by_strategy` | object | P&L broken down by execution strategy |
| `pnl_by_vol_regime` | object | P&L broken down by volatility regime |
| `pnl_by_asset` | object | P&L broken down by asset |

The `pnl_by_*` objects all have the same shape: `{"count": int, "pnl_cents": int, "wins": int}`

---

### 16. Recent Simulated Trades

| Field | Type |
|-------|------|
| `recent_simulated_trades` | array of objects (max 10, newest first) |

```json
{
  "ticker": "KXXRP15M-26FEB220945-45",
  "asset": "XRP",
  "evaluation_time": "2026-02-22T14:41:14Z",
  "market_price": 85,
  "edge": 0.043972,
  "calibrated_prob": 0.893972,
  "strategy": "MAKER_PATIENT",
  "position_size": 1,
  "kelly_f": 0.060664,
  "z_score": -1.7749,
  "vol_regime": "normal",
  "status": "settled",
  "market_result": "no",
  "counterfactual_pnl": -86,
  "settled_time": "2026-02-22T14:45:37Z",
  "breakeven_wr": 0.89,
  "expected_value": -1.98,
  "drawdown_scaler": 0.85,
  "ask_depth": 48,
  "best_ask_source": "orderbook",
  "ofa_confidence": "none"
}
```

| Sub-field | Type | Description |
|-----------|------|-------------|
| `market_price` | int | Entry price in cents (85 = 85¢) |
| `edge` | float | calibrated_prob - market_price/100 |
| `calibrated_prob` | float (0-1) | Model's estimated win probability |
| `strategy` | string or null | `"WAIT"`, `"MAKER_PATIENT"`, `"MAKER_AGGRESSIVE"`, `"TAKER_NOW"` |
| `position_size` | int or null | Contracts |
| `kelly_f` | float or null | Quarter-Kelly fraction |
| `z_score` | float or null | Standard deviations from threshold |
| `vol_regime` | string or null | `"normal"` or `"elevated"` |
| `status` | string | `"pending"` or `"settled"` |
| `market_result` | string or null | `"yes"`, `"no"`, `"all_yes"`, `"all_no"` (null if pending) |
| `counterfactual_pnl` | int or null | P&L in cents (null if pending). Win = `+(100 - entry_price) * count - fee`. Loss = `-(entry_price * count) - fee` |
| `settled_time` | string or null | ISO8601 when market resolved |
| `breakeven_wr` | float or null | `market_price / 100`. Win rate needed to break even |
| `expected_value` | float or null | EV in cents: `prob*(100-ask) - (1-prob)*ask - fee` |
| `drawdown_scaler` | float or null | Position sizing multiplier from drawdown protection |
| `ask_depth` | int or null | Contracts available at best ask price |
| `best_ask_source` | string or null | `"orderbook"` or `"market_nbbo"` |
| `ofa_confidence` | string or null | `"high"`, `"moderate"`, `"low"`, `"none"` |

---

### 17. Strategy & Session Stats

| Field | Type | Example |
|-------|------|---------|
| `strategy_breakdown` | object | `{"WAIT": 0, "MAKER_PATIENT": 0, "MAKER_AGGRESSIVE": 0, "TAKER_NOW": 0}` |
| `session_stats` | object | See below |

```json
{
  "total_opportunities_found": 0,
  "total_markets_scanned": 860,
  "evaluation_rate": 36.4,
  "uptime_minutes": 23.6,
  "last_opportunity_timestamp": null
}
```

| Sub-field | Type | Description |
|-----------|------|-------------|
| `total_opportunities_found` | int | Markets that passed all filters |
| `total_markets_scanned` | int | Total evaluations this session |
| `evaluation_rate` | float | Markets scanned per minute |
| `uptime_minutes` | float | Session uptime |
| `last_opportunity_timestamp` | string or null | ISO8601 of last tradeable opportunity |

---

### 18. Asset Performance

| Field | Type |
|-------|------|
| `asset_performance` | object keyed by asset |

```json
{
  "BTC": {
    "opportunities_found": 0,
    "times_selected": 0,
    "times_rejected": 0,
    "selection_rate": 0.0
  }
}
```

Per-asset trade stats: trades, wins, losses, P&L, avg_edge. With `ONE_ASSET_PER_WINDOW = False`, multiple assets can trade per window.

---

### 19. Recent Opportunities

| Field | Type |
|-------|------|
| `recent_opportunities` | array of objects (max 20) |

```json
{
  "ticker": "KXBTC15M-26FEB221545-45",
  "asset": "BTC",
  "seconds_to_close": 120.5,
  "best_ask": 89,
  "edge_bps": 350,
  "chosen_strategy": "MAKER_PATIENT",
  "rejection_reason": null,
  "ts": "2026-02-22T15:42:30Z"
}
```

`edge_bps`: Edge in basis points (350 = 3.5%). Null if rejected before edge calc.
`rejection_reason`: Null if passed all filters. Otherwise: `"price_out_of_range"`, `"insufficient_edge"`, `"zero_sizing"`, `"strategy_wait"`.

---

### 20. Rejection Summary & Settlements

| Field | Type | Description |
|-------|------|-------------|
| `rejection_summary` | object | Counts of z-score rejections keyed by reason string |
| `pending_settlements` | int | Markets awaiting settlement result |

Note: rejection_summary keys have sanitized decimals (`.`→`_`) due to Firebase key rules. Example: `"|z_score|=8_1 > 8_0 — ..."`: `20`.

---

## Data Types Quick Reference

**Monetary:** All prices/costs in **cents** (int). Balances in **dollars** (float).
**Probabilities:** 0-1 float range.
**Volatility:** Per-5-second scale floats (~0.0001-0.001 typical).
**Timestamps:** ISO8601 UTC strings ending in `Z`.
**Assets:** `"BTC"`, `"ETH"`, `"SOL"`, `"XRP"` (always these four).
**Strategies:** `"WAIT"`, `"MAKER_PATIENT"`, `"MAKER_AGGRESSIVE"`, `"TAKER_NOW"`.
**Vol regimes:** `"normal"`, `"elevated"`.
**Bot statuses:** `"ERROR"`, `"TRADING"`, `"SCANNING"`, `"IDLE"`, `"UNKNOWN"`.

---

## Additional Sections (Not Detailed Above)

The following sections are also pushed but were added after the initial brief:

### 21. Risk Metrics (`risk_metrics`)
Max drawdown (% and $), Sharpe ratio, total P&L, avg P&L per trade, profit factor.

### 22. Execution Quality (`execution_quality`)
Avg/median/min/max fill latency, session fill count, maker fill rate.

### 23. Real Trade Analytics (`real_trade_analytics`)
Breakdowns by asset, price bucket, strategy, hour-of-day. Best/worst trade. Cumulative P&L time series.

### 24. Calibration Diagnostics (`calibration`)
Active method, Brier scores, min sample thresholds for Platt/Beta Cal/BLR.

### 25. NIG Distribution (`nig_distribution`)
Per-asset NIG parameters (a, b, loc, scale) from `dist_config.json`.

### 26. EGARCH Estimation (`egarch_estimation`)
EGARCH(1,1) model parameters and convergence metrics. Promoted to live trading.

### 27. EGARCH Blend (`egarch_blend`)
MZ R²-weighted blend weights per asset, R², QLIKE scores, observation counts. Promoted to live trading.

### 29. RK Adaptive Diagnostics (`rk_adaptive_diagnostics`)
Realized Kernel adaptive bandwidth (H*) metrics per asset.

### 30. Counterfactual Analysis (`counterfactual_analysis`)
- `by_stage` — P&L by filter stage (candidate vs observation_trade)
- `by_bucket` — P&L by price bucket (80-84, 85-89, 90-94, 95-99)
- `money_left_on_table_cents` / `bullets_dodged_cents` / `net_filter_value_cents`

### 31. Ask Distribution (`ask_distribution`)
Distribution of market ask prices in buckets, sweet spot analysis.

### 32. Kalshi Order Flow (`kalshi_order_flow`)
Shadow mode Kalshi-native orderbook signals: imbalance, depth velocity, spread convergence. Per-ticker detail.

### 33. Execution Engine (`execution_engine`)
Comprehensive execution health:
- WebSocket status (connected, subscribed tickers, cached orderbooks)
- Active order queue position and execution method
- Session counters: amend attempts/successes, IOC fills/unfilled, WS/REST fills, post_only rejections
- Escalation funnel and amend success rate
- Strategy distribution
- **Health alerts**: auto-detects broken WS fills, broken IOC, post-only storms, zero fills
- `health_ok` flag

### 34. Orderbook Visibility (`orderbooks`)
Per-asset orderbook snapshots: best ask/bid, spread, depth, age, staleness.

### 35. Balance History (`balance_history`, `balance_history_4h`)
1-minute interval balance snapshots (last hour) and downsampled 4-hour history.

### 36. Convergence Velocity (`convergence_velocity`)
Per-asset ask price convergence velocity.

### 37. Observation Mode Flag (`observation_mode`)
Boolean indicating whether bot is in observation or live mode.

---

## Dashboard Section Suggestions

1. **Header bar** — `bot_status` indicator (color-coded), `uptime_seconds`, `timestamp` (freshness)
2. **Balance card** — `current_balance`, `starting_balance`, `peak_balance`, `daily_pnl_cents`, `daily_pnl_pct`, `drawdown_kelly_mult`
3. **Spot prices strip** — `spot_prices` for all 4 assets, auto-updating
4. **Volatility panel** — `current_volatility` per asset: regime badge, blended_rv, IV/RV spread, jump indicators
5. **Filter funnel** — `filter_funnel` as stacked bar/sankey per asset showing where markets get filtered
6. **Active order** — `active_order` details when non-null, elapsed time progress bar
7. **Execution engine** — `execution_engine` health alerts, session counters, escalation funnel
8. **Recent trades** — `recent_trades` with enrichment (strategy, edge, latency)
9. **Cross-exchange** — `cross_exchange` prices, premia, consensus direction, `feed_health` status dots
10. **Order flow** — `order_flow` signals per asset, confidence badges
11. **Rate limits** — `rate_limits` gauge showing API pressure
12. **Session stats** — `session_stats`, `strategy_breakdown`, `asset_performance`
13. **EGARCH diagnostics** — `egarch_estimation`, `egarch_blend` live model metrics
14. **Counterfactual** — `counterfactual_analysis` money left vs bullets dodged
