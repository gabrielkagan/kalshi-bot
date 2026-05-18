{{ config(
    materialized='external',
    location=var('silver_root') ~ '/coinbase_matches/utc_date=' ~ var('target_date') ~ '/data.parquet',
    format='parquet'
) }}

SELECT
        {{ envelope_cols('coinbase_matches_v1') }},
        json_extract_string(_raw, '$.product_id') AS product_id,
        CAST(json_extract(_raw, '$.sequence') AS BIGINT) AS sequence,
        CAST(json_extract(_raw, '$.trade_id') AS BIGINT) AS trade_id,
        json_extract_string(_raw, '$.maker_order_id') AS maker_order_id,
        json_extract_string(_raw, '$.taker_order_id') AS taker_order_id,
        json_extract_string(_raw, '$.side') AS side,
        CAST(json_extract_string(_raw, '$.size')  AS DOUBLE) AS size,
        CAST(json_extract_string(_raw, '$.price') AS DOUBLE) AS price,
        CAST(json_extract_string(_raw, '$.time')  AS TIMESTAMP) AS exchange_time
FROM read_json_auto(
    '{{ bronze_glob("coinbase_ws", "matches") }}',
    format='newline_delimited',
    compression='zstd',
    columns={
        {{ bronze_columns() }}
    }
)
