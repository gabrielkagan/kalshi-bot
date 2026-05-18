{{ config(
    materialized='external',
    location=var('silver_root') ~ '/coinbase_ticker/utc_date=' ~ var('target_date') ~ '/data.parquet',
    format='parquet'
) }}

SELECT
        {{ envelope_cols('coinbase_ticker_v1') }},
        json_extract_string(_raw, '$.product_id')       AS product_id,
        CAST(json_extract(_raw, '$.sequence') AS BIGINT) AS sequence,
        CAST(json_extract_string(_raw, '$.price')        AS DOUBLE) AS price,
        CAST(json_extract_string(_raw, '$.best_bid')     AS DOUBLE) AS best_bid,
        CAST(json_extract_string(_raw, '$.best_bid_size') AS DOUBLE) AS best_bid_size,
        CAST(json_extract_string(_raw, '$.best_ask')     AS DOUBLE) AS best_ask,
        CAST(json_extract_string(_raw, '$.best_ask_size') AS DOUBLE) AS best_ask_size,
        json_extract_string(_raw, '$.side')              AS side,
        CAST(json_extract_string(_raw, '$.time')         AS TIMESTAMP) AS exchange_time,
        CAST(json_extract(_raw, '$.trade_id')            AS BIGINT) AS trade_id,
        CAST(json_extract_string(_raw, '$.last_size')    AS DOUBLE) AS last_size,
        CAST(json_extract_string(_raw, '$.open_24h')     AS DOUBLE) AS open_24h,
        CAST(json_extract_string(_raw, '$.volume_24h')   AS DOUBLE) AS volume_24h,
        CAST(json_extract_string(_raw, '$.low_24h')      AS DOUBLE) AS low_24h,
        CAST(json_extract_string(_raw, '$.high_24h')     AS DOUBLE) AS high_24h,
        CAST(json_extract_string(_raw, '$.volume_30d')   AS DOUBLE) AS volume_30d
FROM read_json_auto(
    '{{ bronze_glob("coinbase_ws", "ticker") }}',
    format='newline_delimited',
    compression='zstd',
    columns={
        {{ bronze_columns() }}
    }
)
