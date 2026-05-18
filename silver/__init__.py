"""silver — off-VPS dbt-style ETL layer (D3.0, ticket 86b9zxc6t).

Reads bronze JSONL.zst chunks from S3 (per D0.3 §2 envelope), normalizes
into typed Parquet, writes to `s3://kalshi-bot-archive/silver/v1/<source>/utc_date=YYYY-MM-DD/`.

Compute target: operator Mac via launchd nightly @ 02:00 local (NOT the bot VPS).
Stack at D3.0: DuckDB only (direct-DuckDB ETL via `silver/scripts/etl_run.py`).
Stack target post-D3.0-fu1: DuckDB + dbt (dbt-duckdb adapter), per D0.3 §13:422-423.

This package has ZERO imports from `bot.*` or `collector.*` — enforced by
`.importlinter` contracts `silver-no-bot` + `silver-no-collector` and the
test file `tests/contracts/test_silver_no_bot_imports.py`.

Plan doc: `kb/decisions/d3-0-silver-foundations-plan.md`.
"""

__all__: list[str] = []
