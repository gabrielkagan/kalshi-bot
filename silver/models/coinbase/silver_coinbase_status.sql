{{ config(
    materialized='external',
    location=var('silver_root') ~ '/coinbase_status/utc_date=' ~ var('target_date') ~ '/data.parquet',
    format='parquet'
) }}

{# coinbase_status preserves arrays as JSON strings; counts COALESCE-to-0
   per plan-doc M6 (NEVER NULL on currency_count/product_count). #}

SELECT
        {{ envelope_cols('coinbase_status_v1') }},
        json_extract(_raw, '$.currencies')::VARCHAR AS currencies_json,
        json_extract(_raw, '$.products')::VARCHAR   AS products_json,
        CAST(COALESCE(json_array_length(json_extract(_raw, '$.currencies')), 0) AS INTEGER) AS currency_count,
        CAST(COALESCE(json_array_length(json_extract(_raw, '$.products')),   0) AS INTEGER) AS product_count
FROM read_json_auto(
    '{{ bronze_glob("coinbase_ws", "status") }}',
    format='newline_delimited',
    compression='zstd',
    columns={
        {{ bronze_columns() }}
    }
)
