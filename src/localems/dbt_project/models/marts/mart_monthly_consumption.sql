{{ config(materialized='view') }}

select
    report_year,
    report_quarter,
    year_quarter,
    report_month,
    year_month,
    energy_carrier,
    "group",
    sub_group,
    object_id,
    object_name,
    unit,
    allocation_type,
    sum(total_consumption) as total_consumption,
    max(max_peak_power_kw) as max_peak_power_kw,
    avg(avg_flow_rate) as avg_flow_rate,
    avg(avg_pressure_bar) as avg_pressure_bar,
    sum(total_burn_duration_min) as total_burn_duration_min,
    count(distinct local_date) as active_days
from {{ ref('mart_daily_consumption') }}
group by
    report_year,
    report_quarter,
    year_quarter,
    report_month,
    year_month,
    energy_carrier,
    "group",
    sub_group,
    object_id,
    object_name,
    unit,
    allocation_type
order by
    year_month,
    energy_carrier,
    "group",
    sub_group,
    object_id,
    allocation_type
