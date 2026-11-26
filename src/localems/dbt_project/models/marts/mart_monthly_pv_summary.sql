{{ config(materialized='view') }}

select
    report_year,
    report_quarter,
    year_quarter,
    report_month,
    year_month,
    site_metering_point_id,
    plant_mode,
    object_id,
    object_name,
    "group",
    sub_group,
    unit,
    round(sum(pv_production_kwh), 4) as pv_production_kwh,
    round(sum(grid_export_kwh), 4) as grid_export_kwh,
    round(sum(grid_import_kwh), 4) as grid_import_kwh,
    round(sum(self_consumption_kwh), 4) as self_consumption_kwh,
    round(sum(total_site_consumption_kwh), 4) as total_site_consumption_kwh,
    round(
        100.0 * sum(self_consumption_kwh) / nullif(sum(pv_production_kwh), 0),
        2
    ) as self_consumption_rate_pct,
    round(
        100.0 * sum(self_consumption_kwh) / nullif(sum(total_site_consumption_kwh), 0),
        2
    ) as autarky_rate_pct,
    sum(export_exceeds_production_intervals) as export_exceeds_production_intervals,
    count(distinct local_date) as active_days
from {{ ref('mart_daily_pv_summary') }}
group by
    report_year,
    report_quarter,
    year_quarter,
    report_month,
    year_month,
    site_metering_point_id,
    plant_mode,
    object_id,
    object_name,
    "group",
    sub_group,
    unit
order by
    year_month,
    site_metering_point_id
