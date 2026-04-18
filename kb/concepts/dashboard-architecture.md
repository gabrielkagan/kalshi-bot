---
status: current
updated: 2026-04-18
tags: [dashboard, supabase, frontend, architecture]
---
# Dashboard Architecture

## Surface
- **URL:** https://gabekagan.io/dashboard/
- **Repo:** `gabrielkagan/gabekagan` (public), branch `gh-pages`, file `dashboard/index.html` (10,322 lines)
- **Hosting:** GitHub Pages
- **Backend data source:** Supabase (project `srbdajecmkjxinmcozxl`)

## Data flow (live)
```
bot.py MainLoop
  └── DashboardSnapshotBuilder._build_snapshot()   [dashboard_snapshot.py, 4,261 lines]
        └── returns dict with 155+ top-level keys
              │
              ▼
  SupabaseSyncer._sync_dashboard()                  [supabase_sync.py]
        └── UPSERT into dashboard_state (id=1, data JSONB, updated_at)  — every 30s
              │
              ▼
  Supabase Realtime: postgres_changes on dashboard_state, filter id=eq.1
              │
              ▼
  dashboard/index.html window update(d)
        └── calls ~30 updateXxx(s) renderers, each reading s.<key> directly
```

## Data flow (historical analytics)
Separate path — **not** through `dashboard_state`. Frontend calls 10 Supabase RPCs on page load:
```
get_analytics_daily_pnl, get_analytics_by_price, get_analytics_calibration,
get_analytics_vol_history, get_analytics_cal_history, get_analytics_edge_realized,
get_analytics_time_of_day, get_analytics_edge_decay,
get_analytics_hourly_cal_drift, get_analytics_window_correlation
```
Each RPC queries materialized views that are refreshed by `supabase_sync._refresh_analytics_views()` every 15 min via `refresh_analytics_views` RPC.

## Incremental sync (separate from snapshot)
`supabase_sync.py` also watermarks and pushes individual tables:
| Local table | Remote table | Watermark |
|---|---|---|
| evaluated_opportunities | evaluations | id (47 explicit cols) |
| rejected_opportunities | rejections | rowid (25 explicit cols) |
| settled_trades | trades | count + settled_at |
| garch_params + egarch_params | volatility_params | replace on run |
| spx_harrv_shadow_signals | same | id |

## Snapshot key inventory (current, from dashboard_snapshot.py)
155+ top-level keys, roughly grouped:

**Core live state (~20):** timestamp, uptime_seconds, current_balance, peak_balance, starting_balance, initial_deposit, drawdown_kelly_mult, balance_history, balance_history_4h, active_positions, resting_orders, actual_pnl_cents, recent_trades, all_products_recent_trades, bot_status, last_error_message, active_order, pending_settlements, observation_mode, disk_free_gb.

**Performance metrics (~15):** win_count, loss_count, win_rate, all_products_win_count, all_products_loss_count, all_products_win_rate, daily_pnl_cents, daily_pnl_pct, consecutive_losses, consecutive_wins, risk_metrics, all_products_risk_metrics, regime_risk_metrics, config_regime_since, real_trade_analytics, regime_trade_analytics, daily_pnl_history, session_stats.

**Model state (~15):** current_volatility, funding_rates, cross_exchange, order_flow, kalshi_order_flow, convergence_velocity, execution_quality, execution_engine, calibration, hourly_calibration, cal_registry, nig_distribution, egarch_estimation, egarch_blend.

**Markets / discovery (~10):** spot_prices, feed_health, seconds_to_next_close, active_windows, filter_funnel, rate_limits, recent_opportunities, trading_config, orderbooks, ask_distribution.

**Shadow blocks (~30):** shadow_variants, shadow_cal_pipeline, fifteenm_shadow, hourly_alt_shadow, spx_harrv_shadow, weather_no_shadow, weather_no_live, weather_observation, hourly_observation, hourly_live, hourly_no_side, hourly_config_a...m, spx_observation, spx_variants, spx_live, sports_observation, sports_strong_config, sports_variants, weekend_discount_shadow, weekend_discount_live, overnight_discount_shadow, overnight_lp_shadow, decided_contract_shadow, decided_contract_live, dc_expansion_shadow, decided_contracts_by_tier, low_price_shadow, no_side_shadow, relaxed_edge_shadow, terminal_momentum_live, bracket_no_live, stc_shadow_counterfactual, sol_pathc_shadow, eth_filter_shadow, stacking_stats.

**Diagnostics (~10):** counterfactual_analysis, stc_performance, calibration_health, edge_integrity, system_health, shadow_comparison, pipeline_completeness, loss_clustering, capital_utilization, calibration_gap, capital_allocation, data_collection, position_health.

## Frontend renderer inventory
`dashboard/index.html` has 129 `function …` definitions. 30+ are `update*/render*/draw*` that each pull specific keys off the snapshot. No typed contract — every renderer does `s.foo || 0`, `s.foo?.bar || '—'` style defensive reads.

## Supabase schema
- `dashboard_state` — single row (id=1), `data JSONB`, `updated_at TIMESTAMPTZ`
- `trades`, `evaluations`, `rejections` — appended, explicit column lists in `supabase_sync.py`
- `sync_watermarks` — per source_table watermark row
- `volatility_snapshots`, `calibration_snapshots` — append-only via `_insert`
- `spx_harrv_shadow_signals` — separate sync, currently broken (see failures/dashboard-drift.md)
- Materialized views for analytics (refreshed every 15 min)

## Known operational characteristics
- 30-second refresh cycle via Supabase Realtime
- Anon key hardcoded in frontend (read-only, public), service key in bot env
- No staleness indicator in UI other than the `LIVE/OFFLINE` pill
- Full snapshot re-sent every 30s — no delta sync for `dashboard_state`
- Reconciliation every 15 min compares local vs remote row counts, re-syncs mismatched days

## Related
- [[failures/dashboard-drift.md]] — current issues (schema drift, 412-trade gap, 155-key mess)
- [[failures/supabase-sync-silent-failure.md]] — 2026-04-04 precedent (SELECT * schema drift)
- [[failures/pnl-reporting-bugs.md]] — rta.cumulative_pnl scope bug
