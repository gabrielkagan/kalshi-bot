# silver/ — off-VPS ETL for the Data Corpus silver tier

D3.0 (ticket [86b9zxc6t](https://app.clickup.com/t/86b9zxc6t), 2026-05-18). First Bit of Phase 2 (Silver).

Plan doc: `kb/decisions/d3-0-silver-foundations-plan.md` (local-only KB).

## What this does

Reads bronze JSONL.zst chunks from `s3://kalshi-bot-archive/bronze/` and writes typed Parquet to `s3://kalshi-bot-archive/silver/v1/<source>/utc_date=YYYY-MM-DD/`.

Five Tier-1 silver sources normalize directly from per-row JSON parsing (decision #3):

| Silver table | Bronze channel | Schema version |
|---|---|---|
| `coinbase_ticker` | `coinbase_ws/ticker` | `coinbase_ticker_v1` |
| `coinbase_matches` | `coinbase_ws/matches` | `coinbase_matches_v1` |
| `coinbase_heartbeat` | `coinbase_ws/heartbeat` | `coinbase_heartbeat_v1` |
| `coinbase_status` | `coinbase_ws/status` | `coinbase_status_v1` |
| `kalshi_market_lifecycle_v2` | `kalshi_ws/market_lifecycle_v2` | `kalshi_market_lifecycle_v2_v1` |

Deferred to D3.1+ (Tier-2 book reconstruction): `coinbase_level2_batch`, `kalshi_orderbook_delta`.
Deferred to D3.3 (Tier-1, simple schema): `kalshi_trade`.

## Compute target

**Operator's Mac via launchd**, NOT the bot VPS. Per D0.3 §13:422 + `feedback_vps_compute_isolation` — silver ETL is CPU/memory-bound and cannot share compute with the bot's 2vCPU/2GB-RAM VPS.

## Stack

- **DuckDB** (`silver/requirements.txt` pins exact version) — reads bronze JSONL.zst via `read_json_auto` + S3 connector, writes Parquet via `COPY`.
- **D3.0 simplification:** ships direct-DuckDB ETL (`silver/scripts/etl_run.py`) implementing the SQL-per-source layer. The dbt project + dbt model files are deferred to a follow-up Bit (file: D3.0-fu1, see "Followups" below). Decision rationale: the Bit-kickoff verify list (plan-doc decision #5) flagged dbt-duckdb's `incremental + partition_by + delete+insert` interaction as uncertain; D3.0 ships path (c) "custom python materialization" implemented directly in DuckDB rather than wrapped in dbt, deferring the dbt-project layering to a clean follow-up Bit with its own adv-gate against the (now-tested) SQL semantics.

The SQL semantics (5-col unique key + per-utc_date partition rewrite + schema-versioned row col) are equivalent across the direct-DuckDB form here and the future dbt form.

## Operator setup (one-time)

```bash
# 1. Install Python deps (requires Python 3.9+)
cd /path/to/kalshi-bot
pip3 install --user -r silver/requirements.txt

# 2. Configure AWS creds for S3 access. Either:
#    a. ~/.aws/credentials with a [silver-etl] profile, plus AWS_PROFILE=silver-etl env, OR
#    b. AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY env vars in the launchd plist.
#    Operator decides per their key-management preference.

# 3. Sanity-check connectivity
python3 -m silver.scripts.etl_run --date 2026-05-18 \
    --bronze-root s3://kalshi-bot-archive/bronze \
    --silver-root s3://kalshi-bot-archive/silver/v1 --verbose

# 4. Install launchd unit
cp silver/launchd/com.kalshi.silver-etl.plist ~/Library/LaunchAgents/
# Edit the plist — replace /Users/CHANGEME/path/to/kalshi-bot with the real path.
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.kalshi.silver-etl.plist
launchctl enable gui/$(id -u)/com.kalshi.silver-etl

# 5. Verify next-fire
launchctl print gui/$(id -u)/com.kalshi.silver-etl | grep next_run
```

## Crontab equivalent (optional alternate)

If you prefer cron over launchd:

```cron
0 2 * * * /path/to/kalshi-bot/silver/scripts/etl_run.sh >> /tmp/silver-etl.log 2>&1
```

Both fire at 02:00 Mac-local. The ETL computes `target_date = (now_utc - 1 day).date()` at runtime — TZ-independent.

## Bit-kickoff verify list (from plan doc, completed at ship time)

1. ✅ **DuckDB version pin** — `silver/requirements.txt` pins `duckdb==1.1.3`. The `dbt-core` + `dbt-duckdb` pins are explicitly DEFERRED to D3.0-fu1 (not pinned in requirements.txt at D3.0; installing them at this Bit would bloat the operator's `pip install` with unused deps). Compatibility matrix for the deferred pin: https://docs.getdbt.com/reference/warehouse-setups/duckdb-setup.
2. ✅ **Materialization fork resolved** — selected path (c) "custom python materialization" implemented directly in DuckDB (not via dbt-duckdb's `incremental + partition_by + delete+insert` combo). Per-`utc_date` partition rewrite via `DROP existing → COPY new` is equivalent to `delete+insert`. Decision #5 semantics (5-col unique key + per-partition rewrite + schema-versioned row col) hold across paths.
3. ⏳ **S3 IAM lifecycle spot-check** — operator should verify `silver/*` lifecycle policy on the live bucket matches Path C templates (Standard → GLACIER_IR @ 90d). Per `STATE_DB_BACKUP_SETUP.md` §2.

## Followups (D3.0-fu / D3.1+)

- **D3.0-fu1 (URGENT):** layer dbt project + dbt model files on top of the direct-DuckDB ETL. Refactor `silver/scripts/etl_run.py` to invoke `dbt run` rather than executing SQL directly. Closes plan-doc Scope item "dbt project + 5 Tier-1 models".
- **D3.0-fu2 (LOW):** S3 IAM lifecycle live-vs-template spot-check on the live bucket.
- **D3.1 (URGENT):** Coinbase `level2_batch` book-reconstruction silver model.
- **D3.2 (URGENT):** Kalshi `orderbook_delta` book-reconstruction silver model.
- **D3.3 (URGENT):** Kalshi `trade` channel silver model (Tier-1 deferred).
- **D3.4 (NORMAL):** silver QA / gap-detection model.
- **D3.5 (NORMAL):** silver-side ETL health monitoring (analog of D1.6).
- **D3.6 (LOW):** investigate `bronze/kalshi_ws/_unrouted/` provenance.
- **D3.7 (LOW):** exploded `silver_coinbase_status_products_v1` + `_currencies_v1`.

## Backfill

- **Single date:** `silver/scripts/etl_run.sh --date 2026-05-15`
- **Date range:** wrap in a shell loop (or future `etl_run.py --date-range` flag).

## Isolation contracts (importlinter)

`silver/` has **zero** `bot.*` or `collector.*` imports, enforced by:
- `[importlinter:contract:silver-no-bot]`
- `[importlinter:contract:silver-no-collector]`
- `tests/contracts/test_silver_no_bot_imports.py` (configparser-level pin + AST defense + mutation test)

This is the same isolation pattern as `kalshi_wire/` (D1.1.5) and `coinbase_wire/` (D2.1).
