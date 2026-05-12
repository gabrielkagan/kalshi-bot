# DB Schema Reference

`state.db` — SQLite, WAL mode, busy_timeout=10000ms. Run `PRAGMA table_info(<table>)` to verify against current code.

**Source of truth (post-Bit-7.1, 2026-05-10):** all `CREATE TABLE` and `ALTER TABLE` statements live in `bot/state.py::StateManager._create_tables`. Pre-Bit-7.1 they were in `bot/_impl.py:559-3165` (the StateManager class body); Sprint 7 extracted the class verbatim with the `_create_tables` schema invariant (zero column deltas — `tests/integration/test_state_extraction.py::test_schema_zero_delta_per_table` is the regression seal across all 17 tables). When schema changes ship, edit `bot/state.py::StateManager._create_tables` AND this doc in the same commit.

This doc is sister Bit 7.2 (ClickUp `86b9vda5u`); refreshed in lock-step with Bit 7.1 per `kb/decisions/bit-7.1-plan-may10.md`.

## settled_trades

| Column | Type | Notes |
|--------|------|-------|
| ticker | TEXT PK | Market ticker |
| event_ticker | TEXT | Event-level ticker |
| asset | TEXT | BTC, ETH, SOL, XRP, HYPE (shadow), DOGE (shadow) |
| market_result | TEXT | Settlement result |
| side | TEXT | yes/no |
| count | INTEGER | Contracts |
| entry_price_cents | INTEGER | Entry price in cents |
| revenue_cents | INTEGER | Settlement revenue |
| fee_cents | INTEGER | Fees paid |
| pnl_cents | INTEGER | **GROSS** PnL in cents (`revenue_cents - count*entry_price_cents`). EXCLUDES fees. For true net use `SUM(pnl_cents - COALESCE(fee_cents, 0))`. See `kb/failures/audit-pnl-fee-omission-apr29.md`. |
| settled_at | TEXT | Settlement timestamp |
| product_type | TEXT | 15m, hourly, spx_hourly, weather, sports |

## evaluated_opportunities

| Column | Type | Notes |
|--------|------|-------|
| id | INTEGER PK | Auto-increment |
| ticker | TEXT | Market ticker |
| event_ticker | TEXT | Event-level ticker |
| asset | TEXT | Asset symbol |
| filter_stage | TEXT | candidate, observation_trade, shadow, edge_too_low, etc. |
| rejection_reason | TEXT | Why rejected (if applicable) |
| evaluation_time | TEXT | When evaluated |
| spot_price | REAL | Underlying price |
| threshold | REAL | Strike threshold |
| volatility | REAL | Vol estimate used |
| market_price | INTEGER | Market price in cents |
| seconds_to_close | REAL | STC at evaluation |
| calibrated_prob | REAL | Final calibrated probability |
| edge | REAL | Edge percentage |
| ofa_adjustment | REAL | Order flow adjustment |
| status | TEXT | open/settled |
| market_result | TEXT | Settlement result (backfilled) |
| counterfactual_pnl | REAL | Simulated PnL |
| product_type | TEXT | 15m, hourly, spx_hourly, weather, sports |
| orderbook_levels_json | TEXT | Top-10 YES ladder JSON `{"yes_bids":[[p,q],...],"yes_asks":[[p,q],...]}` from `_extract_book_levels`. Auto-filled from `_scan_ob_cache` (10s freshness gate; stale → NULL). |

## rejected_opportunities

| Column | Type | Notes |
|--------|------|-------|
| ticker | TEXT PK | Market ticker |
| event_ticker | TEXT | Event-level ticker |
| asset | TEXT | Asset symbol |
| rejection_reason | TEXT | Full descriptive string (NOT short labels) |
| rejection_time | TEXT | When rejected |
| z_score | REAL | Z-score at rejection |
| spot_price | REAL | Underlying price |
| threshold | REAL | Strike threshold |
| volatility | REAL | Vol estimate |
| market_price | INTEGER | Market price in cents |
| seconds_to_close | REAL | STC at rejection |
| calibrated_prob | REAL | Calibrated probability |
| status | TEXT | open/settled |
| product_type | TEXT | 15m, hourly, spx_hourly, weather, sports |
| sigma_winsorize | REAL | (Sprint B Bit B.1a, 2026-05-12) Winsorized `spot_distance_to_strike_sigma` clipped to ±`SIGMA_WINSOR_ABS_CAP=25.0` via `bot.helpers.derived_features.apply_sigma_winsor`. Mirrors `scripts/cal_mlp/features.py` train-time clip. |
| hour_sin | REAL | (B.1a) Cyclic 24h embedding of UTC hour. Derived via `bot.helpers.derived_features.compute_hour_sin_cos` — lock-step with cal_mlp canonical formula (`scripts/cal_mlp/features.compute_hour_features`, A.1b). |
| hour_cos | REAL | (B.1a) Cyclic 24h embedding of UTC hour. Lock-step with cal_mlp. |
| prob_breakeven_gap | REAL | (B.1a) `calibrated_prob − market_price/100`. NULL when either input is missing (e.g. `price_out_of_range_early` rejections that fire before calibrated_prob compute). |
| vol_regime | TEXT | (B.1a) 'normal' / 'elevated' from `vol_est["regime"]` at rejection time. NULL when rejection fires before vol_est is built. |
| data_provenance | TEXT | (B.1a) 'live_ws' for live-bot inserts (mirrors `evaluated_opportunities.data_provenance` Sprint A.2 / commit f26a611). B.1a-fu2 (2026-05-12) adds 'backfill_b1a_fu2' for rows touched by `scripts/backfill/wave1_derived_cols.py` (stamped only when the backfill actually computed at least one Wave 1 cell; rows with all-live-written cells keep prov=NULL). |
| orderbook_levels_json | TEXT | (B.1a) Top-N YES ladder JSON via `_get_fresh_ob_ladder` (10s freshness gate; stale → NULL). NULL on `no_orderbook` rejections — correct, the gate fires precisely because the ladder is absent. |

## Other tables

- **positions** — Open position tracking (ticker PK, asset, side, count, avg_price_cents, status)
- **pending_orders** — In-flight order tracking (order_id PK, ticker, side, action, count, price_cents, status)
- **garch_params** — Persisted GARCH parameters per asset
- **egarch_params** — Persisted EGARCH(1,1) parameters per asset
- **sports_shadow_log** — Sports comeback shadow signals (game_id, sport, league, teams, comeback_prob, edge, market_result, pnl_cents)
- **fifteenm_shadow_signals** — 15M shadow A1/A2/A3 signals (in fifteenm_shadow.py)
- **hourly_alt_shadow_signals** — Hourly alternative shadow signals (in hourly_alt_shadow.py)
- **position_price_observations** — Held-position WS tick logs (ticker, observation_time, spot_price, yes_ask_cents, yes_bid_cents, **orderbook_levels_json**). 15M monitor reads ladder from `_scan_ob_cache` (freshness-gated); weather monitor REST-fetches via `client.get_orderbook`.
- **order_lifecycle_snapshots** — Per-event book snapshots at order lifecycle transitions. Columns: `id INTEGER PK, order_id TEXT, ticker TEXT, event_type TEXT CHECK IN ('submit','fill','partial_fill','cancel'), observation_time TEXT, orderbook_levels_json TEXT, source TEXT`. Source = strategy name; execution tier recoverable via order_id join. Indexed on (order_id) and (ticker, observation_time). Failure counter: `StateManager._lifecycle_snapshot_failures`.
- **historical_replay_calmlp** — Phase 2 HYPE/DOGE replay-backfill corpus (ticket `86b9wy7v3`, 2026-05-12; Mac-local SQLite — operator-supplied via `scripts/backfill/hype_doge_replay_backfill.py --db PATH`. DO NOT point at production `state.db` to avoid contaminating audit queries — `*.db` is gitignored via top-level pattern). 19 cols: `ticker TEXT, evaluation_time TEXT, asset TEXT CHECK IN ('HYPE','DOGE'), strike_cents INTEGER, close_time TEXT, open_time TEXT, raw_prob REAL, calibrated_prob REAL, blended_prob REAL, spot_at_evaluation REAL, sigma_at_evaluation REAL, hour_sin REAL, hour_cos REAL, prob_breakeven_gap REAL, sigma_winsorize REAL, result TEXT CHECK IN ('yes','no'), settlement_value INTEGER, data_provenance TEXT, replay_run_ts INTEGER, PRIMARY KEY (ticker, evaluation_time)`. PK enables idempotent `INSERT OR REPLACE`. `data_provenance='replay_phase2_v1'` stamps every row. Phase 2 v1 honest-NULLs: `blended_prob` (no HYPE/DOGE trained cal_mlp predictor exists at either deployment site — production `_calmlp_predictors` cache constructs only BTC/ETH/SOL/XRP; cross-asset transfer eval is fu1 of 86b9wy7v3), `prob_breakeven_gap` (no historical Kalshi orderbook). Expected ~9,800 rows post-backfill (~4,897 HYPE + ~4,897 DOGE; per Phase 1 corpus survey — `kb/findings/hype-doge-kalshi-market-history-may12.md` — 4,911 settled pre-T1 markets/asset, with ~14 per asset skipped at replay time due to floor_strike=None per the Apr-13 incident parity). Writer: `scripts/backfill/hype_doge_replay_backfill.py::replay_market()`. Consumer: sister `86b9wy15n` calibration health check.
- **cohort_attribution_daily** — Money Printer Roadmap Phase 1 P1.1 (ticket `86b9x3kgd`, 2026-05-12). Per-cohort daily aggregate of 15M crypto admits + cell-block-routed rows. 23 cols, composite PK `(cohort_date, asset, product_type, strategy, price_band_5c, stc_band_60s, cell_block_stage)`. Materialized nightly at 13:07 UTC by `scripts/audit/cohort_attribution_nightly.py` calling `bot.helpers.cohort_attribution.run_aggregation`. Cohort key dims: asset (BTC/ETH/SOL/XRP live; HYPE/DOGE shadow), product_type ('15m' for now; '1h' joins post-Phase-3 hourly promotion), strategy (18 distinct), price_band_5c (`market_price // 5`), stc_band_60s (`min(int(stc // 60), 11)`; band 11 = 660s+ tail), cell_block_stage (the canonical 5-stage UNION-set, including `'candidate'` baseline). Rolling 30d + 7d windows on n / wr / cf_pnl / mean_cal_prob / cal_gap. Honest-NULL: rows with `market_result NOT IN ('yes','no')` filtered at SQL level — un-settled rows are excluded, NOT folded in as 0-outcomes. Alert state bookkeeping cols (`alert_state`, `last_alert_time`) populated by inline write-back protocol (design § Alert state write-back protocol option a; parameter-injection via `alerts_module`). Sister P1.3 ships `bot.helpers.cohort_alerts` for the trigger primitives; graceful fallback writes `'quiet'` everywhere until P1.3 lands. Indexed on (cohort_date) + (cohort_date, cf_pnl_30d_dollars). Idempotent on rerun (INSERT OR REPLACE on composite PK). Storage estimate: ~600 cells/day × 23 cols ≈ 6 MB/year. Design: `kb/decisions/cohort-measurement-design-may12.md` (LOCAL-only).
- **order_decision_snapshots** — Sister table to order_lifecycle_snapshots (Sprint B Bit B.2b, 2026-05-12, ticket `86b9vfzr2`; shipped as **Phase-1**). ONE row per maker-vs-taker route DECISION (NOT per event). Columns: `id INTEGER PK, decision_id TEXT NOT NULL, ticker TEXT NOT NULL, asset TEXT NOT NULL, decision_time TEXT NOT NULL, decision_type TEXT NOT NULL CHECK IN ('maker_first','taker_first','escalate','shadow'), orderbook_levels_json TEXT, spot_price REAL, seconds_to_close REAL, vol_regime TEXT, source TEXT, followup_ticks_json TEXT`. `decision_id` is a UUID4 hex seeded on the candidate dict at the top of `OrderExecutor.execute()` and reused by `_escalate_to_taker_inner` so the maker→escalation pair shares it (join `WHERE decision_id=X ORDER BY id` reconstructs the route sequence). `followup_ticks_json` is an opportunistic 30s post-decision tick stream appended by `StateManager.append_decision_followup_tick()` (single-writer, JSON list, capped at `DECISION_FOLLOWUP_MAX_TICKS=8`; option (c) per ticket — no separate table). **Phase-1 status (2026-05-12):** `followup_ticks_json` is currently `NULL` on every row — Phase-1 B.2b shipped the schema + helper + 13 decision-emit sites + retention, but the `OrderExecutor.tick()` wire-up that ACTUALLY populates the tick stream is deferred to follow-up ticket `86b9wgetr` (B.2b-fu1). Until that ships, queries on `followup_ticks_json` will return all NULLs by design, not a bug. Indexed on (decision_id) and (ticker, decision_time). Auto-fills orderbook_levels_json from `_scan_ob_cache` via the freshness gate (stale → NULL, never lie). Retention: 90 days via `StateManager.prune_old_decision_snapshots()` invoked from `MainLoop._log_daily_summary` (daily housekeeping). Volume estimate ~500-1000 rows/day × ~3KB = ~270 MB / 90d. Decision-point emit sites in `bot/executor.py`: `_execute_hourly_taker`, `_execute_weather_no_taker`, `_execute_hourly_no_taker`, `_execute_dc_taker`, `_execute_tm_taker`, `_execute_lpne_taker`, `_execute_bracket_no_taker`, SOL taker-first override (`execute()` ~L903), direct-taker <180s (`execute()` ~L1076), maker tier-1 (`execute()` ~L1250), maker tier-2 degraded (`execute()` ~L1235), post-only-taker tier-3 escalation (`execute()` ~L1162), and `_escalate_to_taker_inner`. Helper: `OrderExecutor._emit_decision_snapshot(candidate, decision_type)` (best-effort; failures logged + swallowed).

## Storage paths

- `state.db` — SQLite (settled_trades, rejected_opportunities, evaluated_opportunities, etc.)
- `opportunity_journal.jsonl` — filter stage tracking
- `scan_journal.jsonl` — per-tick scan summaries (~330MB/day)
- `fill_model_journal.jsonl` — order lifecycle (maker + taker IOC) for ML fill prediction

### `fill_model_journal.jsonl` field reference

Writer: `OrderExecutor._log_fill_model_sample` in `bot/executor.py`. One row per
order outcome (`outcome ∈ {"filled","canceled","partial_filled","expired"}`).
Sprint B Bit B.2a (2026-05-12, ticket 86b9vfznd) audited the production
journal and reclassified NULL-prone columns. The current surface is:

| Column | Type | Applicability predicate |
|---|---|---|
| `type` | `"fill_model_sample"` literal | always present |
| `ts`, `ticker`, `asset`, `outcome` | identity | always non-NULL |
| `fill_latency_s` | float \| null | non-NULL iff `outcome == "filled"`; NULL on canceled/expired by design |
| `fill_source` | str \| null | `"websocket"` / `"rest_poll"` set by maker fill paths; `"ioc_inline"` set by writer for `outcome=filled AND is_taker=True`; NULL on cancel/expired |
| `price_cents`, `count`, `post_only`, `fair_value`, `offset_cents` | submission context | always non-NULL when candidate has `best_yes_ask` |
| `seconds_to_close`, `vol_regime`, `blended_rv` | market context at submission | non-NULL (sourced from candidate) |
| `ask_depth`, `total_ob_depth`, `bid_depth`, `spread_at_submit` | OB context | NULL iff `ob_snapshot_source != "scanner"` OR (for `spread`/`bid_depth`) the resting bid book was empty at scan |
| `ob_snapshot_source` | `"scanner"` \| `"addon_empty"` \| `"missing"` | predicate column for the four OB fields above. `"addon_empty"` = confirmation_addon / dip_addon path (no fresh scanner OB mid-execution). `"missing"` = candidate had no ob_snapshot at all. |
| `z_score`, `edge`, `kelly_f` | signal context | non-NULL (sourced from candidate) |
| `queue_position_final` | int \| null | non-NULL iff maker order survived ≥5s AND `client.get_queue_position` succeeded |
| `queue_position_polled` | bool | predicate column: True iff the polling loop fired at least once for this order. Distinguishes "filled before first poll" from "polled but Kalshi returned nothing" |
| `execution_method` | `"maker"` \| `"ioc"` \| `"cancel_replace_ioc"` \| ... | non-NULL |
| `entry_path` | str | strategy entry tag (`"maker"`, `"direct_taker"`, `"tm_taker"`, `"sol_taker_override"`, `"confirmation_addon"`, `"dip_addon"`, ...) |
| `cancel_reason` | str \| null | non-NULL iff `outcome ∈ {"canceled","partial_filled","expired"}` |
| `elapsed_seconds`, `ws_connected`, `maker_only_threshold` | bookkeeping | always non-NULL |

**Removed in B.2a** (were 100% NULL in 10,210 production rows):
- `queue_position_initial` — never written anywhere in code.
- `convergence_velocity` — computed by scanner into strategy-helper dicts but
  never propagated to the `candidate` dict; the writer was reading a key
  that never existed.

Pre-B.2a rows on disk retain those two fields with `null` values; consumers
must tolerate their absence on post-B.2a rows.
