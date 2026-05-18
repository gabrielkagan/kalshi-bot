{{ config(
    materialized='external',
    location=var('silver_root') ~ '/coinbase_heartbeat/utc_date=' ~ var('target_date') ~ '/data.parquet',
    format='parquet'
) }}

SELECT
        {{ envelope_cols('coinbase_heartbeat_v1') }},
        json_extract_string(_raw, '$.product_id') AS product_id,
        CAST(json_extract(_raw, '$.sequence') AS BIGINT) AS sequence,
        CAST(json_extract(_raw, '$.last_trade_id') AS BIGINT) AS last_trade_id,
        CAST(json_extract_string(_raw, '$.time') AS TIMESTAMP) AS exchange_time
FROM read_json_auto(
    '{{ bronze_glob("coinbase_ws", "heartbeat") }}',
    format='newline_delimited',
    compression='zstd',
    columns={
        {{ bronze_columns() }}
    }
)
