# silver/ — off-VPS dbt-driven ETL for the Data Corpus silver tier

D3.0 (ticket [86b9zxc6t](https://app.clickup.com/t/86b9zxc6t), 2026-05-18). First Bit of Phase 2 (Silver).
D3.0-fu1 (ticket [86ba0a2k8](https://app.clickup.com/t/86ba0a2k8), 2026-05-18) layers the dbt project on top of the D3.0 direct-DuckDB ETL.

Plan doc: `kb/decisions/d3-0-silver-foundations-plan.md` (local-only KB).

## What this does

Reads bronze JSONL.zst chunks from `s3://kalshi-bot-archive/bronze/` and writes typed Parquet to `s3://kalshi-bot-archive/silver/v1/<source>/utc_date=YYYY-MM-DD/data.parquet` via a dbt project.

Five Tier-1 silver sources normalize directly from per-row JSON parsing (decision #3):

| Silver table | Bronze channel | Schema version | dbt model |
|---|---|---|---|
| `coinbase_ticker` | `coinbase_ws/ticker` | `coinbase_ticker_v1` | `silver_coinbase_ticker` |
| `coinbase_matches` | `coinbase_ws/matches` | `coinbase_matches_v1` | `silver_coinbase_matches` |
| `coinbase_heartbeat` | `coinbase_ws/heartbeat` | `coinbase_heartbeat_v1` | `silver_coinbase_heartbeat` |
| `coinbase_status` | `coinbase_ws/status` | `coinbase_status_v1` | `silver_coinbase_status` |
| `kalshi_market_lifecycle_v2` | `kalshi_ws/market_lifecycle_v2` | `kalshi_market_lifecycle_v2_v1` | `silver_kalshi_market_lifecycle_v2` |

Deferred to D3.1+ (Tier-2 book reconstruction): `coinbase_level2_batch`, `kalshi_orderbook_delta`.
Deferred to D3.3 (Tier-1, simple schema): `kalshi_trade`.

## Compute target

**Operator's Mac via launchd**, NOT the bot VPS. Per D0.3 §13:422 + `feedback_vps_compute_isolation` — silver ETL is CPU/memory-bound and cannot share compute with the bot's 2vCPU/2GB-RAM VPS.

## Stack (post-D3.0-fu1)

- **DuckDB 1.1.3** — reads bronze JSONL.zst via `read_json_auto` + S3 connector, writes Parquet via `COPY`.
- **dbt-core 1.8.7 + dbt-duckdb 1.8.4** — orchestrates the 5 Tier-1 models. Each model uses `materialized='external'` with `location` parameterized via `var('target_date')` + `var('silver_root')` so a single `dbt run --vars '{target_date: YYYY-MM-DD}'` writes that day's Parquet for every model.
- **Materialization fork resolved (D3.0-fu1 sandbox):** chose path (c) from the plan-doc Bit-kickoff verify list item #2 — `materialized='external'` with parameterized `location`. DuckDB's `COPY ... TO 'file.parquet'` overwrites the target file atomically; rerunning for the same `target_date` produces row-set-stable output (idempotency semantics per decision #5 — same input bronze → same output rows; Parquet byte-equality is NOT guaranteed because writers may choose different row-group boundaries / dict encodings / metadata across runs).

## Repo layout

```
silver/
├── dbt_project.yml         # dbt project marker; binds to profile 'silver'
├── profiles.yml.example    # operator-facing profile template; runtime wrapper generates its own tmp profiles.yml
├── requirements.txt        # duckdb + dbt-core + dbt-duckdb + zstandard pinned
├── README.md               # this file
├── __init__.py             # makes silver/ an importable Python package (for scripts/etl_run.py)
├── .gitignore              # excludes target/, logs/, profiles.yml runtime, dbt_packages/
├── macros/
│   └── envelope_columns.sql  # shared macros: envelope_cols, bronze_glob, bronze_columns
├── models/
│   ├── coinbase/
│   │   ├── silver_coinbase_ticker.sql
│   │   ├── silver_coinbase_matches.sql
│   │   ├── silver_coinbase_heartbeat.sql
│   │   └── silver_coinbase_status.sql
│   └── kalshi/
│       └── silver_kalshi_market_lifecycle_v2.sql
├── scripts/
│   ├── __init__.py
│   ├── etl_run.py          # subprocess wrapper: pre-checks bronze, invokes `dbt run`, queries Parquet for rowcount
│   └── etl_run.sh          # shell wrapper sourced by launchd
└── launchd/
    └── com.kalshi.silver-etl.plist  # operator-installed launchd unit (gui/<uid> domain)
```

The `silver/` directory is both a dbt project root AND an importable Python package. The two coexist because dbt looks for `*.sql` and `dbt_project.yml`, while Python imports `silver.scripts.etl_run` via the `__init__.py` markers. dbt's `models/` + `macros/` dirs have no `__init__.py`, so Python ignores them.

## Operator setup (one-time)

```bash
# 1. Install Python deps (requires Python 3.9+)
cd /path/to/kalshi-bot
pip3 install --user -r silver/requirements.txt

# 2. Configure AWS creds for S3 access. Either:
#    a. ~/.aws/credentials with a [silver-etl] profile, plus AWS_PROFILE=silver-etl env, OR
#    b. AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY env vars in the launchd plist.
#    Operator decides per their key-management preference.

# 3. (Optional) Customize the dbt profile. The runtime wrapper writes its own
#    tmp profiles.yml each run — you only need to set up a global profile if
#    you want to invoke `dbt` commands directly outside the wrapper:
#       cp silver/profiles.yml.example ~/.dbt/profiles.yml

# 4. Sanity-check end-to-end
python3 -m silver.scripts.etl_run --date 2026-05-18 \
    --bronze-root s3://kalshi-bot-archive/bronze \
    --silver-root s3://kalshi-bot-archive/silver/v1 --verbose

# 5. Install launchd unit
cp silver/launchd/com.kalshi.silver-etl.plist ~/Library/LaunchAgents/
# Edit the plist — replace /Users/CHANGEME/path/to/kalshi-bot with the real path.
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.kalshi.silver-etl.plist
launchctl enable gui/$(id -u)/com.kalshi.silver-etl

# 6. Verify next-fire
launchctl print gui/$(id -u)/com.kalshi.silver-etl | grep next_run
```

## Crontab equivalent (optional alternate)

If you prefer cron over launchd:

```cron
0 2 * * * /path/to/kalshi-bot/silver/scripts/etl_run.sh >> /tmp/silver-etl.log 2>&1
```

Both fire at 02:00 Mac-local. The ETL computes `target_date = (now_utc - 1 day).date()` at runtime — TZ-independent.

## Upstream dependency: bronze must be flowing

Silver reads bronze JSONL.zst at `s3://kalshi-bot-archive/bronze/`. The 4 Coinbase silver sources (`coinbase_ticker/matches/heartbeat/status`) require D2.5 Coinbase bronze day-zero to have happened (operator `systemctl start kalshi-coinbase-collector` on the VPS; see root CLAUDE.md D2.5 entry). The Kalshi silver source (`kalshi_market_lifecycle_v2`) requires D1.5 Kalshi bronze day-zero. Until both day-zeros land, the corresponding silver sources will SKIP per the wrapper's bronze-chunk pre-check (test #3 SKIP-on-empty semantics) — that's by design, not a bug. Verify bronze is flowing before debugging silver "no output" cases.

## Direct dbt invocation (debugging / one-off backfill)

The wrapper at `silver/scripts/etl_run.py` is the canonical entry. For ad-hoc debugging you can invoke dbt directly — useful for running a single model or compiling without executing. dbt requires a file named exactly `profiles.yml` in the directory pointed to by `DBT_PROFILES_DIR` (it does NOT read `profiles.yml.example`), so set up a one-shot profiles dir first:

```bash
# One-time profiles setup for direct invocation
mkdir -p /tmp/silver-profiles
cp silver/profiles.yml.example /tmp/silver-profiles/profiles.yml

# Compile-only (no execution)
DBT_PROFILES_DIR=/tmp/silver-profiles dbt compile --project-dir silver \
    --vars '{"target_date": "2026-05-18", "bronze_root": "s3://kalshi-bot-archive/bronze", "silver_root": "s3://kalshi-bot-archive/silver/v1"}'

# Run a single model
DBT_PROFILES_DIR=/tmp/silver-profiles dbt run --project-dir silver \
    --select silver_coinbase_ticker \
    --vars '{"target_date": "2026-05-18", "bronze_root": "s3://kalshi-bot-archive/bronze", "silver_root": "s3://kalshi-bot-archive/silver/v1"}'
```

The automated wrapper at `silver/scripts/etl_run.py` does NOT use `DBT_PROFILES_DIR` — it generates its own tmp profiles.yml per run and passes `--profiles-dir <tmp>` to avoid races between concurrent operator runs. The `cp` recipe above is purely for interactive debugging.

## Bit-kickoff verify list (from plan doc, status at D3.0-fu1 ship time)

1. ✅ **DuckDB + dbt-core + dbt-duckdb version pin** — `silver/requirements.txt` pins `duckdb==1.1.3`, `dbt-core==1.8.7`, `dbt-duckdb==1.8.4`. Compatibility matrix: https://docs.getdbt.com/reference/warehouse-setups/duckdb-setup.
2. ✅ **Materialization fork resolved** — selected path (c) `materialized='external'` with parameterized `location`. Sandbox prototype at D3.0-fu1 kickoff verified that `dbt run --vars '{target_date: ...}'` produces the expected partitioned Parquet at `silver/v1/<table>/utc_date=YYYY-MM-DD/data.parquet`. Idempotency via DuckDB `COPY ... TO` overwrite — semantically equivalent to the plan-doc decision #5 `delete+insert` per UTC-day partition. (Note: idempotency here is **row-set stable** — `SELECT * ORDER BY collector_seq` produces identical rows across reruns — NOT byte-stable; Parquet writers may produce different bytes across runs due to row-group/dict-encoding/metadata choices.)
3. ⏳ **S3 IAM lifecycle spot-check** — operator should verify `silver/*` lifecycle policy on the live bucket matches Path C templates (Standard → GLACIER_IR @ 90d). Per `STATE_DB_BACKUP_SETUP.md` §2.
4. ⏳ **CI gate for silver integration tests** — `.github/workflows/{test,deploy}.yml` do NOT currently `pip install -r silver/requirements.txt`, so `tests/integration/test_silver_etl_pipeline.py` skips silently in CI (the skip helper in `_import_etl_or_skip` checks for dbt-on-PATH). Filed as D3.0-fu1.1 followup ([86ba0a2tg](https://app.clickup.com/t/86ba0a2tg)) to extend the CI install path. Until that lands, silver behavioral coverage is dev-machine only; structural contracts at `tests/contracts/test_silver_dbt_project.py` + import-isolation at `tests/contracts/test_silver_no_bot_imports.py` still run in CI without dbt.

## Followups (D3.0-fu / D3.1+)

- **D3.0-fu1.1** ([86ba0a2tg](https://app.clickup.com/t/86ba0a2tg), NORMAL): CI gate — extend `.github/workflows/{test,deploy}.yml` to `pip install -r silver/requirements.txt` so silver integration tests run in CI rather than skip silently.
- **D3.0-fu1.2** ([86ba0a323](https://app.clickup.com/t/86ba0a323), LOW): S3 zero-row Parquet cleanup — wrapper currently leaves a zero-row Parquet at S3 when bronze pre-check is deferred (TRUE-unconditional on S3). Add boto3-based S3 pre-check OR post-run S3 DELETE.
- **D3.0-fu1.3** ([86ba0a8nc](https://app.clickup.com/t/86ba0a8nc), LOW): `silver_root` `Path` collapse on `s3://` paths — pre-existing D3.0 bug; `Path("s3://...")` becomes `s3:/...` and breaks the post-run rowcount query against S3. Affects S3 paths only; D3.0 base has the same bug.
- **D3.0-fu2 (LOW):** S3 IAM lifecycle live-vs-template spot-check on the live bucket.
- **D3.1 (URGENT):** Coinbase `level2_batch` book-reconstruction silver model.
- **D3.2 (URGENT):** Kalshi `orderbook_delta` book-reconstruction silver model.
- **D3.3 (URGENT):** Kalshi `trade` channel silver model (Tier-1 deferred).
- **D3.4 (NORMAL):** silver QA / gap-detection model (dbt schema tests can layer on once the project is established).
- **D3.5 (NORMAL):** silver-side ETL health monitoring (analog of D1.6).
- **D3.6 (LOW):** investigate `bronze/kalshi_ws/_unrouted/` provenance.
- **D3.7 (LOW):** exploded `silver_coinbase_status_products_v1` + `_currencies_v1`.

## Backfill

- **Single date (any of 5 models):** `silver/scripts/etl_run.sh --date 2026-05-15`
- **Date range:** wrap in a shell loop (or future `etl_run.py --date-range` flag).
- **Single model only (debugging):** invoke dbt directly with `--select <model_name>` per "Direct dbt invocation" §.
- **Full-refresh / nuclear backfill:** there's no separate `dbt run --full-refresh` path for silver — the wrapper always rewrites the target partition (per-date `external` location), so calling `etl_run.sh --date <D>` for each historical day reproduces the silver for that range.

## Isolation contracts (importlinter)

`silver/` has **zero** `bot.*` or `collector.*` imports, enforced by:
- `[importlinter:contract:silver-no-bot]`
- `[importlinter:contract:silver-no-collector]`
- `tests/contracts/test_silver_no_bot_imports.py` (configparser-level pin + AST defense + mutation test)
- `tests/contracts/test_silver_dbt_project.py` (D3.0-fu1 dbt-project structural pins)

This is the same isolation pattern as `kalshi_wire/` (D1.1.5) and `coinbase_wire/` (D2.1).
