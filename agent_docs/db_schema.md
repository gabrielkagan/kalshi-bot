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

## Storage paths

- `state.db` — SQLite (settled_trades, rejected_opportunities, evaluated_opportunities, etc.)
- `opportunity_journal.jsonl` — filter stage tracking
- `scan_journal.jsonl` — per-tick scan summaries (~330MB/day)
- `fill_model_journal.jsonl` — maker order lifecycle for ML fill prediction
