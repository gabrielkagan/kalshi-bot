# DB Schema Reference

`state.db` — SQLite, WAL mode, busy_timeout=10000ms. Run `PRAGMA table_info(<table>)` to verify against current code.

**Source of truth (post-Bit-7.1, 2026-05-10):** all `CREATE TABLE` and `ALTER TABLE` statements live in `bot/state.py::StateManager._create_tables`. Pre-Bit-7.1 they were in `bot/_impl.py:559-3165` (the StateManager class body); Sprint 7 extracted the class verbatim with the `_create_tables` schema invariant (zero column deltas — `tests/integration/test_state_extraction.py::test_schema_zero_delta_per_table` is the regression seal across all 17 tables). When schema changes ship, edit `bot/state.py::StateManager._create_tables` AND this doc in the same commit.

This doc is sister Bit 7.2 (ClickUp `86b9vda5u`); refreshed in lock-step with Bit 7.1 per `kb/decisions/bit-7.1-plan-may10.md`.

## settled_trades

| Column | Type | Notes |
|--------|------|-------|
| ticker | TEXT PK | Market ticker |
| event_ticker | TEXT | Event-level ticker |
| asset | TEXT | BTC, ETH, SOL, XRP, HYPE, DOGE, BNB (all live; HYPE/DOGE T4 P2.3 2026-05-14, BNB T4 P2.4 2026-05-19 ticket 86b9zmj37 — `filter_stage='bnb_shadow'` gate at scanner ~6195 preserved as DEAD-but-revert-kill-switch; flip `BNB_15M_SHADOW=True` in `bot/constants.py` to revert) |
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
| config_snapshot_id | INTEGER | (Ticket 86b9zkp8p, 2026-05-17) **Advisory pointer** to `config_snapshots(id)` — NO SQL FOREIGN KEY constraint (sqlite `ALTER TABLE ADD COLUMN` limitation; ON DELETE / referential integrity NOT enforced at DB level). Captures EXACTLY which config produced this decision (sha256 over `bot/constants.py` + `bot/config.py` + `market_config.py` + sorted-key JSON of tracked env flags + git HEAD). Phase-1 stamped at `MainLoop.__init__` and propagated to every CALLER (scanner, executor) via `self._ml.config_snapshot_id`; sports engine stamps its thread-local conn at first `_get_db_conn()`. UPSERT-stable via `COALESCE` so re-emitted stages preserve their original decision-time snapshot. NULL on pre-Bit rows + any test/backfill caller that doesn't pass the kwarg. See `bot/helpers/config_snapshot.py` + `bot/CLAUDE.md` "config_snapshot_id schema chain". |
| tm_shadow_kelly_ct | INTEGER | (Ticket 86ba0v7fc, Sim C, 2026-05-19) **Shadow-only** counterfactual Kelly contract count for TM trades. Computed via `bot.helpers.tm_sweep.tm_shadow_kelly_contracts_with_bound` at the TM eval site using `cal_mlp_p_mean` (with `raw_prob` fallback) under half-Kelly + $100 abs-loss bound + per-asset TM_ASSET_RISK_CAPS. NEVER consumed by production sizing — pure logging surface for the Sim C analysis. NULL when both probability signals are NULL (`bound_hit='null_prob'`) or when the helper raises (exception-swallowed in scanner). UPSERT-stable via `COALESCE` so the FIRST stamp on a re-emitted row survives. See `kb/decisions/tm-half-kelly-shadow-plan.md`. |
| tm_shadow_kelly_prob | REAL | (Sim C) The probability value the helper used: `cal_mlp_p_mean` when non-NULL, else `raw_prob_fallback`. NULL ⇔ both signals NULL ⇔ `tm_shadow_kelly_ct IS NULL` AND `tm_shadow_kelly_bound_hit='null_prob'`. |
| tm_shadow_kelly_fraction | REAL | (Sim C) Fractional-Kelly multiplier applied (default 0.50 = half-Kelly per `bot.constants.TM_SHADOW_KELLY_FRACTION`). Stamped as a column rather than a constant so a future Bit that varies the fraction per-asset / per-band has the join key in-row. |
| tm_shadow_kelly_bound_hit | TEXT | (Sim C) Which constraint bound the size: `'kelly'` (Kelly's natural ct was smallest), `'abs_loss'` ($100 abs-loss bound binds), `'asset_cap'` (TM_ASSET_RISK_CAPS[asset] binds), `'null_prob'` (no probability signal — ct=NULL), or `'raw_fallback'` (cal_mlp_p_mean NULL but raw_prob present — the bound_hit semantics are overridden to surface fallback provenance; the numerical cap is still applied). |
| spot_staleness_seconds | REAL | (Ticket 86ba1wrcg, Bit S.1, 2026-05-21) Seconds between the last Coinbase WS ticker frame that populated `CoinbaseFeed._prices[asset]` and the scan-tick evaluation time. Measured via `time.monotonic()` (process-wide clock, no NTP/leap-second jumps). Populated for **every** Coinbase scan-path insert (candidate, decided_contract*, insufficient_edge, price_out_of_range, silent_spot_none, … all 115+ insert sites reached after the `_feed.get_price_with_ts(asset)` read) via the `StateManager._scan_spot_staleness_cache[asset]` auto-fill — mirrors the `_scan_cx_gap_cache` precedent. NULL only when (a) the scan path didn't read from CoinbaseFeed — SPX/weather/sports route through other engines and don't populate the cache (note: hourly DOES populate the cache because hourly markets fall through the same Coinbase `else` branch in `OpportunityScanner.scan()` as 15M; the `_pt in (None, "15m", "hourly")` reach is intentional), (b) the asset slot was popped because CoinbaseFeed has never seen a tick for the asset this scan tick (warmup), or (c) the caller is a backfill / test path that supplies neither the kwarg nor the cache. Observability-only — Bit S.3 (deferred, ticket `86ba1wrka` under umbrella `86ba1wrad`) will add the production gate `MAX_SPOT_STALENESS_SECONDS_BY_ASSET` once 24h+ of distribution data informs per-asset thresholds. UPSERT-stable via `COALESCE` so the FIRST staleness reading on a re-emitted row survives. Discovered 2026-05-21: Coinbase BNB-USD has 34% 1-min gaps over May 9-21 — the bot was using stale-by-minutes prices on illiquid windows. S.2 RCA found zero settled trades with proxy staleness ≥120s (implicit selection effect via downstream gates) but the visibility is still load-bearing for S.3 threshold selection. See `kb/decisions/bit-s-1-spot-staleness-instrumentation-plan.md` + `kb/findings/spot-staleness-pnl-attribution.md` + umbrella `86ba1wrad`. |
| rti_synthetic | REAL | (Ticket 86ba64h2w, B2b-1, 2026-05-28; cid=142) **SHADOW-ONLY.** Decision-time multi-venue synthetic CFB-shape Real-Time-Index reconstructed in-bot from 4-venue L2 books (Coinbase/Kraken/Bitstamp/Gemini) by `bot/feeds/synthetic_rti_feed.py::SyntheticRTIFeed` (which wraps the validated `bot.feeds.synthetic_rti.compute_synthetic_rti` aggregator). Staged once per scan tick into `StateManager._scan_rti_cache[asset]` from `SyntheticRTIFeed.get_cached_synthetic(asset)` (an O(1) read of the feed's off-hot-path sampler cache; the ~7ms/asset compute runs on the sampler daemon, NOT in `scanner.scan()` — keeps the SCAN_BODY_SLOW budget intact) and auto-filled across the Coinbase scan-path insert sites — mirrors the `_scan_cx_gap_cache` / `spot_staleness_seconds` precedent. **NEVER read by any decision path** (the zero-live-decision-change invariant, pinned by `tests/contracts/test_synthetic_rti_shadow_invariant.py`). Paired with the live single-venue Coinbase `spot_price` + `market_price` + settled outcome already on the row → the corpus Bit 3 retrains on. NULL when the kill-switch `SYNTHETIC_RTI_ENABLED` is OFF (the default), on non-Coinbase scan paths (SPX/weather/sports), when the sampler cache is stale (WS outage), or for backfill/test callers. UPSERT-stable via `COALESCE`. See `kb/decisions/b2b-1-core-shadow-plan.md` + `kb/decisions/b2b-multi-venue-signal-program-plan.md`. |
| rti_constituent_count | INTEGER | (B2b-1; cid=143) Number of venues that contributed to `rti_synthetic` this tick (survived the per-venue staleness-drop + Kraken CRC32 desync-drop + CFB potentially-erroneous filter). Same auto-fill/NULL semantics as `rti_synthetic`. |
| rti_confidence | REAL | (B2b-1; cid=144) `rti_constituent_count / SyntheticRTIFeed.n_expected_venues(asset)` — the fraction of the asset's CFB-constituent venue set the feed actually sourced this tick (∈ (0,1]; e.g. BTC expects 4, a 3-venue reconstruction → 0.75). Same auto-fill/NULL semantics as `rti_synthetic`. |

## config_snapshots

Per-decision config-hash table. Ticket 86b9zkp8p (2026-05-17). Replay = look up
the snapshot row → restore the exact config bundle → re-run the decision.

| Column | Type | Notes |
|--------|------|-------|
| id | INTEGER PK | Auto-increment. Advisory-pointer target for `evaluated_opportunities.config_snapshot_id` + `rejected_opportunities.config_snapshot_id` (no SQL FK constraint — sqlite ALTER TABLE limitation). |
| config_hash | TEXT UNIQUE | sha256 over `constants_sha + "|" + config_sha + "|" + market_config_sha + "|" + env_flags_json + "|" + git_head_sha`. Composite key — drift in any one input rotates the hash. UNIQUE constraint + `INSERT OR IGNORE` means re-stamping an unchanged config returns the existing id (no row duplication on restart). |
| captured_at | TEXT | ISO-8601 UTC of FIRST stamp. Subsequent INSERT-or-IGNORE returns the same id (won't update this field). |
| git_head_sha | TEXT | Best-effort `git rev-parse HEAD`. "unknown" if git unavailable / no commits / 2s subprocess timeout exceeded. NULL never happens (deterministic sentinel). |
| constants_sha | TEXT | sha256 of `bot/constants.py` file bytes. Missing file → sha256(b''). |
| config_sha | TEXT | sha256 of `bot/config.py` file bytes. |
| market_config_sha | TEXT | sha256 of `market_config.py` file bytes. |
| env_flags_json | TEXT | sorted-key JSON of tracked env-var flags that are CURRENTLY SET: `CALMLP_ENABLED`, `WEATHER_NO_SIDE_LIVE`, `HOURLY_NO_SIDE_LIVE`, `BRACKET_NO_ENABLED`, `MEXC_FEED_ENABLED`, `BINANCE_FEED_ENABLED`, `BAND_CALIBRATION_DISABLED_CELLS`. Unset flags are OMITTED (keeps legacy-environment hash stable when a new flag is added later — only the live setting rotates the hash). |

Indexes: `idx_config_snapshots_hash` (config_hash). Phase-1 captures the snapshot
once per process boot at `MainLoop.__init__`; mid-day mutation re-capture is
Phase-2 (followup ticket).

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
| config_snapshot_id | INTEGER | (Ticket 86b9zkp8p, 2026-05-17) **Advisory pointer** to `config_snapshots(id)` (no SQL FK constraint; same caveats as `evaluated_opportunities.config_snapshot_id` above). Every rejection-decision row carries the snapshot that produced it, so regime-filter replay of rejected-side cohorts is deterministic. |

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
- **historical_replay_calmlp** — Phase 2 HYPE/DOGE replay-backfill corpus (ticket `86b9wy7v3`, 2026-05-12; Mac-local SQLite — operator-supplied via `scripts/backfill/hype_doge_replay_backfill.py --db PATH`. DO NOT point at production `state.db` to avoid contaminating audit queries — `*.db` is gitignored via top-level pattern). 20 cols (P2.3.b-fu2 2026-05-13 ticket `86b9xtam7` added `threshold REAL` to preserve sub-cent strike precision; legacy `strike_cents INTEGER` retained for back-compat with pre-fix HYPE corpus): `ticker TEXT, evaluation_time TEXT, asset TEXT CHECK IN ('HYPE','DOGE'), strike_cents INTEGER, threshold REAL, close_time TEXT, open_time TEXT, raw_prob REAL, calibrated_prob REAL, blended_prob REAL, spot_at_evaluation REAL, sigma_at_evaluation REAL, hour_sin REAL, hour_cos REAL, prob_breakeven_gap REAL, sigma_winsorize REAL, result TEXT CHECK IN ('yes','no'), settlement_value INTEGER, data_provenance TEXT, replay_run_ts INTEGER, PRIMARY KEY (ticker, evaluation_time)`. PK enables idempotent `INSERT OR REPLACE`. `ensure_schema()` migrates pre-fu2 DBs in place via `ALTER TABLE ADD COLUMN threshold REAL` when the column is absent. `data_provenance='replay_phase2_v1'` stamps every row. Phase 2 v1 honest-NULLs: `blended_prob` (no HYPE/DOGE trained cal_mlp predictor exists at either deployment site — production `_calmlp_predictors` cache constructs only BTC/ETH/SOL/XRP; cross-asset transfer eval is fu1 of 86b9wy7v3), `prob_breakeven_gap` (no historical Kalshi orderbook). Pre-fu2 HYPE rows have NULL `threshold` (`extract_data_replay.build_feature_frame` falls back to `strike_cents/100.0` per `kb/findings/replay-backfill-strike-precision-bug-may13.md`). Expected ~9,800 rows post-backfill (~4,897 HYPE + ~4,897 DOGE; per Phase 1 corpus survey — `kb/findings/hype-doge-kalshi-market-history-may12.md` — 4,911 settled pre-T1 markets/asset, with ~14 per asset skipped at replay time due to floor_strike=None per the Apr-13 incident parity). Writer: `scripts/backfill/hype_doge_replay_backfill.py::replay_market()`. Consumer: sister `86b9wy15n` calibration health check.
- **cohort_attribution_daily** — Money Printer Roadmap Phase 1 P1.1 (ticket `86b9x3kgd`, 2026-05-12). Per-cohort daily aggregate of 15M crypto admits + cell-block-routed rows. 23 cols, composite PK `(cohort_date, asset, product_type, strategy, price_band_5c, stc_band_60s, cell_block_stage)`. Materialized nightly at 13:07 UTC by `scripts/audit/cohort_attribution_nightly.py` calling `bot.helpers.cohort_attribution.run_aggregation`. Cohort key dims: asset (BTC/ETH/SOL/XRP live; HYPE/DOGE shadow), product_type ('15m' for now; '1h' joins post-Phase-3 hourly promotion), strategy (18 distinct), price_band_5c (`market_price // 5`), stc_band_60s (`min(int(stc // 60), 11)`; band 11 = 660s+ tail), cell_block_stage (the canonical 5-stage UNION-set, including `'candidate'` baseline). Rolling 30d + 7d windows on n / wr / cf_pnl / mean_cal_prob / cal_gap. Honest-NULL: rows with `market_result NOT IN ('yes','no')` filtered at SQL level — un-settled rows are excluded, NOT folded in as 0-outcomes. Alert state bookkeeping cols (`alert_state`, `last_alert_time`) populated by inline write-back protocol (design § Alert state write-back protocol option a; parameter-injection via `alerts_module`). Sister P1.3 ships `bot.helpers.cohort_alerts` for the trigger primitives; graceful fallback writes `'quiet'` everywhere until P1.3 lands. Indexed on (cohort_date) + (cohort_date, cf_pnl_30d_dollars). Idempotent on rerun (INSERT OR REPLACE on composite PK). Storage estimate: ~600 cells/day × 23 cols ≈ 6 MB/year. Design: `kb/decisions/cohort-measurement-design-may12.md` (LOCAL-only).
- **phantom_corrections** — B4 (ticket `86b9zudcc`, 2026-05-18) retroactive ledger of `settled_trades` rows whose local count diverged from Kalshi truth. **Created on-demand** by `scripts/audit/phantom_pnl_audit.py::ensure_phantom_table` (not part of `bot/state.py::_create_tables` schema-of-record because it's an audit-only artifact). **Auto-populated hourly on the VPS** by the cron-driven `scripts/ops/phantom_reconcile_monitor.py` (TBD, 2026-05-19) using `audit_run_id = "auto-YYYY-MM-DD"` (day-granular), or on-demand by the audit script directly with `--run-id <name>`. Columns: `id INTEGER PK AUTOINCREMENT, audit_run_id TEXT NOT NULL, ticker TEXT NOT NULL, side TEXT, detected_at TEXT NOT NULL, local_count INTEGER NOT NULL, kalshi_count INTEGER NOT NULL, delta_count INTEGER NOT NULL, local_pnl_cents INTEGER NOT NULL, corrected_pnl_cents INTEGER NOT NULL, delta_pnl_cents INTEGER NOT NULL, kalshi_revenue_cents INTEGER, avg_price_cents INTEGER, market_result TEXT, audit_window_days INTEGER, notes TEXT`, `UNIQUE(audit_run_id, ticker, side)`. `*_pnl_cents` columns are GROSS (mirror `settled_trades.pnl_cents` semantics; fees excluded — net consumers re-join `settled_trades.fee_cents` downstream). `delta_count = local − kalshi` (positive = over-count); `delta_pnl_cents = corrected − local` (positive = local overstated the loss / understated the win). Indexed on `(ticker)`. Idempotent re-runs use `audit_run_id` as the dedup namespace; day-granular `auto-` IDs keep the table to AT MOST one row per (day, ticker, side) so downstream LEFT JOIN consumers don't see row multiplication on persistent phantoms.
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
