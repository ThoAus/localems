with raw_parquet as (
    select
        timestamp,
        interval_minutes,
        messpunkt_id,
        objekt_id,
        sparte,
        channel,
        value,
        unit,
        reading_type,
        quality,
        peak_power_kw,
        flow_rate,
        pressure_bar,
        burn_duration_min,
        source,
        ingested_at
    from read_parquet(
        '{{ env_var("EM_TOOLS_PARQUET_GLOB", "../data/parquet/sparte=*/year=*/*.parquet") }}',
        hive_partitioning = false
    )
),

enriched as (
    select
        timestamp as timestamp_utc,
        (timestamp at time zone '{{ var("report_timezone", "Europe/Rome") }}') as local_timestamp,
        cast((timestamp at time zone '{{ var("report_timezone", "Europe/Rome") }}') as date) as local_date,
        cast(extract(year from (timestamp at time zone '{{ var("report_timezone", "Europe/Rome") }}')) as integer) as report_year,
        cast(extract(quarter from (timestamp at time zone '{{ var("report_timezone", "Europe/Rome") }}')) as integer) as report_quarter,
        concat(
            cast(extract(year from (timestamp at time zone '{{ var("report_timezone", "Europe/Rome") }}')) as varchar),
            '-Q',
            cast(extract(quarter from (timestamp at time zone '{{ var("report_timezone", "Europe/Rome") }}')) as varchar)
        ) as year_quarter,
        interval_minutes,
        messpunkt_id,
        objekt_id as raw_objekt_id,
        lower(sparte) as sparte,
        channel,
        cast(value as double) as value,
        unit,
        lower(reading_type) as reading_type,
        lower(quality) as quality,
        cast(peak_power_kw as double) as peak_power_kw,
        cast(flow_rate as double) as flow_rate,
        cast(pressure_bar as double) as pressure_bar,
        cast(burn_duration_min as double) as burn_duration_min,
        source,
        ingested_at
    from raw_parquet
)

select * from enriched
