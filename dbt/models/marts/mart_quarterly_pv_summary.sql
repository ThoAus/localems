with pv as (
    select * from {{ ref('int_pv_interval_balance') }}
),

objects as (
    select * from {{ ref('stg_objects') }}
)

select
    p.report_year,
    p.report_quarter,
    p.year_quarter,
    p.site_metering_point_id,
    p.plant_mode,
    p.object_id,
    o.object_name,
    o."group",
    o.sub_group,
    p.unit,
    round(sum(p.pv_production_kwh), 4) as pv_production_kwh,
    round(sum(p.grid_export_kwh), 4) as grid_export_kwh,
    round(sum(p.grid_import_kwh), 4) as grid_import_kwh,
    round(sum(p.self_consumption_kwh), 4) as self_consumption_kwh,
    round(sum(p.total_site_consumption_kwh), 4) as total_site_consumption_kwh,
    round(
        100.0 * sum(p.self_consumption_kwh) / nullif(sum(p.pv_production_kwh), 0),
        2
    ) as self_consumption_rate_pct,
    round(
        100.0 * sum(p.self_consumption_kwh) / nullif(sum(p.total_site_consumption_kwh), 0),
        2
    ) as autarky_rate_pct,
    sum(case when p.is_export_exceeding_production then 1 else 0 end) as export_exceeds_production_intervals,
    count(distinct p.local_date) as active_days
from pv p
left join objects o
    on p.object_id = o.object_id
group by
    p.report_year,
    p.report_quarter,
    p.year_quarter,
    p.site_metering_point_id,
    p.plant_mode,
    p.object_id,
    o.object_name,
    o."group",
    o.sub_group,
    p.unit
order by
    p.report_year,
    p.report_quarter,
    p.site_metering_point_id
