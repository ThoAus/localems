with measurements as (
    select *
    from {{ ref('stg_measurements') }}
    where reading_type = 'interval'
),

meter_tree as (
    select * from {{ ref('int_metering_point_tree') }}
),

objects as (
    select * from {{ ref('stg_objects') }}
)

select
    m.timestamp_utc,
    m.local_timestamp,
    m.local_date,
    m.report_year,
    m.report_quarter,
    m.year_quarter,
    m.interval_minutes,
    m.metering_point_id,
    mt.parent_metering_point_id,
    coalesce(mt.root_metering_point_id, m.metering_point_id) as root_metering_point_id,
    coalesce(mt.meter_level, 0) as meter_level,
    coalesce(mt.is_main_meter, true) as is_main_meter,
    coalesce(mt.tree_path, m.metering_point_id) as tree_path,
    coalesce(mt.meter_type, 'smartmeter') as meter_type,
    coalesce(mt.object_id, m.raw_object_id) as object_id,
    m.energy_carrier,
    m.channel,
    m.value,
    m.unit,
    m.quality,
    m.peak_power_kw,
    m.flow_rate,
    m.pressure_bar,
    m.burn_duration_min,
    m.source,
    o.object_name,
    o."group",
    o.sub_group
from measurements m
left join meter_tree mt
    on m.metering_point_id = mt.metering_point_id
left join objects o
    on coalesce(mt.object_id, m.raw_object_id) = o.object_id
