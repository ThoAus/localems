with measurements as (
    select *
    from {{ ref('stg_measurements') }}
    where reading_type = 'interval'
),

messpunkte as (
    select * from {{ ref('stg_messpunkte') }}
),

objekte as (
    select * from {{ ref('stg_objekte') }}
)

select
    m.timestamp_utc,
    m.local_timestamp,
    m.local_date,
    m.report_year,
    m.report_quarter,
    m.year_quarter,
    m.interval_minutes,
    m.messpunkt_id,
    coalesce(mp.objekt_id, m.raw_objekt_id) as objekt_id,
    m.sparte,
    m.channel,
    m.value,
    m.unit,
    m.quality,
    m.peak_power_kw,
    m.flow_rate,
    m.pressure_bar,
    m.burn_duration_min,
    m.source,
    o.objekt_name,
    o.objekt_typ,
    o.ebene_1,
    o.ebene_2
from measurements m
left join messpunkte mp
    on m.messpunkt_id = mp.messpunkt_id
left join objekte o
    on coalesce(mp.objekt_id, m.raw_objekt_id) = o.objekt_id
