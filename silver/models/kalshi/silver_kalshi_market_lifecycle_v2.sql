{{ config(
    materialized='external',
    location=var('silver_root') ~ '/kalshi_market_lifecycle_v2/utc_date=' ~ var('target_date') ~ '/data.parquet',
    format='parquet'
) }}

{# Kalshi market_lifecycle_v2 wide-table; event-type cols NULL-able.
   R1-C1: bronze channel name retains `_v2` suffix; silver table mirrors.
   Permissive event_type enum per plan-doc test #5 (unknown event_types
   route to a future QA model at D3.4, not crash). #}

SELECT
        {{ envelope_cols('kalshi_market_lifecycle_v2_v1') }},
        CAST(json_extract(_raw, '$.sid') AS INTEGER) AS sid,
        CAST(json_extract(_raw, '$.seq') AS BIGINT)  AS wire_seq,
        json_extract_string(_raw, '$.msg.event_type')     AS event_type,
        json_extract_string(_raw, '$.msg.market_ticker')  AS market_ticker,
        CAST(json_extract(_raw, '$.msg.floor_strike')     AS DOUBLE) AS floor_strike,
        json_extract_string(_raw, '$.msg.yes_sub_title')  AS yes_sub_title,
        CAST(json_extract(_raw, '$.msg.determination_ts') AS BIGINT) AS determination_ts,
        json_extract_string(_raw, '$.msg.result')         AS result,
        CAST(json_extract_string(_raw, '$.msg.settlement_value') AS DOUBLE) AS settlement_value
FROM read_json_auto(
    '{{ bronze_glob("kalshi_ws", "market_lifecycle_v2") }}',
    format='newline_delimited',
    compression='zstd',
    columns={
        {{ bronze_columns() }}
    }
)
