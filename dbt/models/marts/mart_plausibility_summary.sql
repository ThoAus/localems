with residuals as (
    select * from {{ ref('int_hierarchy_residuals') }}
),

objects as (
    select * from {{ ref('stg_objects') }}
)

select
    r.report_year,
    r.report_quarter,
    r.year_quarter,
    r.energy_carrier,
    r.metering_point_id,
    r.meter_level,
    r.is_main_meter,
    r.root_metering_point_id,
    r.tree_path,
    r.object_id,
    o.object_name,
    o.object_class,
    o.level_1,
    o.level_2,
    r.channel,
    r.unit,
    count(distinct r.local_date) as active_days,
    sum(r.interval_count) as observed_intervals,
    count(distinct r.local_date) * 96 as expected_intervals_active_days,
    round(
        100.0 * sum(r.interval_count) / nullif(count(distinct r.local_date) * 96, 0),
        2
    ) as completeness_pct_active_days,
    sum(r.negative_interval_count) as negative_interval_count,
    sum(case when r.is_negative_residual then 1 else 0 end) as negative_residual_days,
    sum(r.gross_value) as total_gross_value,
    sum(r.submeter_deduction_value) as total_submeter_deduction,
    sum(r.residual_value) as total_residual_value,
    max(r.max_peak_power_kw) as max_peak_power_kw
from residuals r
left join objects o
    on r.object_id = o.object_id
group by
    r.report_year,
    r.report_quarter,
    r.year_quarter,
    r.energy_carrier,
    r.metering_point_id,
    r.meter_level,
    r.is_main_meter,
    r.root_metering_point_id,
    r.tree_path,
    r.object_id,
    o.object_name,
    o.object_class,
    o.level_1,
    o.level_2,
    r.channel,
    r.unit
order by
    r.report_year,
    r.report_quarter,
    r.energy_carrier,
    r.tree_path
