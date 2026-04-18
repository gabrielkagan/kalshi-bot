---
status: current
updated: 2026-04-18
tags: [dashboard, performance, sqlite, snapshot]
---
# Dashboard Snapshot — Single-Scan Pattern

## Why
The win/loss + risk-metrics section of `dashboard_snapshot.py._build_snapshot()` historically issued **11 separate queries** against `settled_trades` per 30s cycle:

| Query | Purpose |
|---|---|
| `WHERE product_type='15m'` (side, result) | 15M win/loss |
| `(no filter)` (side, result) | all-products win/loss |
| `WHERE settled_at >= today AND product_type='15m'` | daily PnL |
| `WHERE product_type='15m' ORDER BY settled_at DESC LIMIT 50` | consecutive streak |
| `WHERE product_type='15m' ORDER BY settled_at` (pnl, fee) | 15M risk stats |
| `GROUP BY DATE` on 15M (inside `_compute_risk_stats`) | 15M daily-aggregated Sharpe |
| `(no filter) ORDER BY settled_at` (pnl, fee) | all-products risk stats |
| `GROUP BY DATE` (no filter) (inside `_compute_risk_stats`) | all-products Sharpe |
| `WHERE product_type='15m' AND settled_at >= regime` (side, result) | regime win/loss |
| `WHERE product_type='15m' AND settled_at >= regime ORDER BY settled_at` (pnl, fee) | regime risk stats |
| `GROUP BY DATE WHERE product_type='15m' AND settled_at >= '...'` | regime Sharpe |

All 11 are subsets of the same underlying `settled_trades` data. The fan-out existed because the three scopes (15M / all-products / regime) were computed independently and each scope's Sharpe was fetched via its own subquery.

## Pattern (2026-04-18)
One scan, in-memory bucketing:

```python
all_settled_rows = conn.execute("""
    SELECT side, market_result, product_type, settled_at,
           DATE(settled_at) AS day,
           (pnl_cents - fee_cents) AS net
    FROM settled_trades
    ORDER BY settled_at
""").fetchall()

settled_15m        = [r for r in all_settled_rows if r["product_type"] == "15m"]
settled_15m_regime = [r for r in settled_15m
                      if r["settled_at"] and r["settled_at"] >= CONFIG_REGIME_SINCE]
# all_settled_rows is already the "all products" bucket
```

`_compute_risk_stats(rows)` was refactored to take the pre-filtered list and compute daily aggregates in memory via a dict keyed on `DATE(settled_at)`.

`daily_pnl_cents` is now `sum(r["net"] for r in settled_15m if r["settled_at"] >= today_midnight)`.

`consecutive_wins/losses` now slices `settled_15m[-50:][::-1]` instead of re-querying with `ORDER BY settled_at DESC LIMIT 50`.

## Invariants preserved
- **Wire format identical.** Snapshot keys (`win_count`, `loss_count`, `win_rate`, `all_products_*`, `regime_risk_metrics`, `daily_pnl_cents`, `consecutive_losses`, etc.) unchanged. Frontend requires no update.
- **Same values.** ISO-8601 string comparison is lexicographic-compatible, so `r["settled_at"] >= today_midnight` in Python matches the SQL `WHERE settled_at >= ?` semantics.
- **Same behavior on empty/null.** `r["net"] is None` and empty-bucket branches still return zeros.

## Cost reduction
- **Queries per snapshot cycle: 11 → 1** (~91% reduction for this section)
- **Effective DB work: 5 full scans → 1 full scan** plus 3 eliminated GROUP BY aggregations
- At 30s snapshot interval × 2,336 rows × 11 queries ≈ 77K rows scanned per minute → ~7K rows/min

## What's still expensive (and why we stopped here)
- `real_trade_analytics` section (line ~876) pulls `settled_at, net, product_type, strategy` and builds the cumulative PnL series for the equity curve. Could merge into the same scan, but would require adding `strategy` and `asset` + `entry_price_cents` columns to the shared SELECT, bloating row size. Deferred.
- Regime-filtered `by_asset` and `by_bucket` subqueries (line ~897). Same deferral rationale.
- These are lower-frequency wins (each runs once per slow-cache TTL = 60s, not every 30s).

## Future direction (Phase B)
The v2 snapshot contract (see `kb/decisions/dashboard-overhaul-plan.md`) will collapse the three-scope duplication at the wire layer: one typed `metrics: {scope_15m, scope_all, scope_regime}` nested object instead of three sibling keys. The single-scan pattern here is the same shape as the v2 compute will need — incremental improvement paid forward.

## Related
- [[concepts/dashboard-architecture.md]]
- [[failures/dashboard-drift.md]]
- [[decisions/dashboard-overhaul-plan.md]]
