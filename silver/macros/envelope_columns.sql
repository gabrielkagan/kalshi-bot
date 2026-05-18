{# silver/ dbt macros (D3.0-fu1, ticket 86ba0a2k8, 2026-05-18).

   Shared building blocks consumed by all 5 Tier-1 silver models. Lifts
   the envelope projection + the bronze-glob pattern out of the
   per-source model SQL, matching plan-doc decision #7 ("envelope
   columns are inherited via a shared dbt macro").

   - envelope_cols(silver_schema_version): emits the 7 D0.3 §2 envelope
     columns (wire_recv_ts, source, conn, channel, collector_seq,
     silver_schema_version, utc_date) projected from the bronze row.
   - bronze_glob(source, channel): emits the date-partitioned glob path
     for `{{ var('bronze_root') }}/<source>/<channel>/year=YYYY/month=MM/day=DD/hour=*/conn=*/*.jsonl.zst`.
     Uses var('target_date') (caller passes the target_date implicitly
     via the dbt-run --vars arg).
#}

{% macro envelope_cols(silver_schema_version) %}
        CAST(_wire_recv_ts AS TIMESTAMP) AS wire_recv_ts,
        _source AS source,
        _conn   AS conn,
        _channel AS channel,
        CAST(_collector_seq AS BIGINT) AS collector_seq,
        '{{ silver_schema_version }}' AS silver_schema_version,
        CAST(_wire_recv_ts AS DATE) AS utc_date
{% endmacro %}

{% macro bronze_glob(source, channel) %}
{%- set parts = var('target_date').split('-') -%}
{{ var('bronze_root') }}/{{ source }}/{{ channel }}/year={{ parts[0] }}/month={{ parts[1] }}/day={{ parts[2] }}/hour=*/conn=*/*.jsonl.zst
{%- endmacro %}

{# bronze_columns: the 6-col DuckDB read_json_auto `columns={...}` spec
   shared across all 5 silver models. Pinning the column types at the
   read seam (vs. inferring) keeps the silver projection stable when
   bronze chunks have schema-drift edges. #}
{% macro bronze_columns() %}
        _wire_recv_ts: 'VARCHAR',
        _source: 'VARCHAR',
        _conn: 'VARCHAR',
        _channel: 'VARCHAR',
        _collector_seq: 'BIGINT',
        _raw: 'VARCHAR'
{% endmacro %}
