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
- **order_decision_snapshots** — Sister table to order_lifecycle_snapshots (Sprint B Bit B.2b, 2026-05-12, ticket `86b9vfzr2`; shipped as **Phase-1**). ONE row per maker-vs-taker route DECISION (NOT per event). Columns: `id INTEGER PK, decision_id TEXT NOT NULL, ticker TEXT NOT NULL, asset TEXT NOT NULL, decision_time TEXT NOT NULL, decision_type TEXT NOT NULL CHECK IN ('maker_first','taker_first','escalate','shadow'), orderbook_levels_json TEXT, spot_price REAL, seconds_to_close REAL, vol_regime TEXT, source TEXT, followup_ticks_json TEXT`. `decision_id` is a UUID4 hex seeded on the candidate dict at the top of `OrderExecutor.execute()` and reused by `_escalate_to_taker_inner` so the maker→escalation pair shares it (join `WHERE decision_id=X ORDER BY id` reconstructs the route sequence). `followup_ticks_json` is an opportunistic 30s post-decision tick stream appended by `StateManager.append_decision_followup_tick()` (single-writer, JSON list, capped at `DECISION_FOLLOWUP_MAX_TICKS=8`; option (c) per ticket — no separate table). **Phase-1 status (2026-05-12):** `followup_ticks_json` is currently `NULL` on every row — Phase-1 B.2b shipped the schema + helper + 13 decision-emit sites + retention, but the `OrderExecutor.tick()` wire-up that ACTUALLY populates the tick stream is deferred to follow-up ticket `86b9wgetr` (B.2b-fu1). Until that ships, queries on `followup_ticks_json` will return all NULLs by design, not a bug. Indexed on (decision_id) and (ticker, decision_time). Auto-fills orderbook_levels_json from `_scan_ob_cache` via the freshness gate (stale → NULL, never lie). Retention: 90 days via `StateManager.prune_old_decision_snapshots()` invoked from `MainLoop._log_daily_summary` (daily housekeeping). Volume estimate ~500-1000 rows/day × ~3KB = ~270 MB / 90d. Decision-point emit sites in `bot/executor.py`: `_execute_hourly_taker`, `_execute_weather_no_taker`, `_execute_hourly_no_taker`, `_execute_dc_taker`, `_execute_tm_taker`, `_execute_lpne_taker`, `_execute_bracket_no_taker`, SOL taker-first override (`execute()` ~L903), direct-taker <180s (`execute()` ~L1076), maker tier-1 (`execute()` ~L1250), maker tier-2 degraded (`execute()` ~L1235), post-only-taker tier-3 escalation (`execute()` ~L1162), and `_escalate_to_taker_inner`. Helper: `OrderExecutor._emit_decision_snapshot(candidate, decision_type)` (best-effort; failures logged + swallowed).

## Storage paths

- `state.db` — SQLite (settled_trades, rejected_opportunities, evaluated_opportunities, etc.)
- `opportunity_journal.jsonl` — filter stage tracking
- `scan_journal.jsonl` — per-tick scan summaries (~330MB/day)
- `fill_model_journal.jsonl` — maker order lifecycle for ML fill prediction
