select
    messpunkt_id,
    objekt_id,
    sparte,
    channel,
    unit,
    local_date,
    report_year,
    report_quarter,
    year_quarter,
    sum(value) as gross_value,
    count(*) as interval_count,
    sum(case when value < 0 then 1 else 0 end) as negative_interval_count,
    max(peak_power_kw) as max_peak_power_kw,
    avg(flow_rate) as avg_flow_rate,
    avg(pressure_bar) as avg_pressure_bar,
    sum(coalesce(burn_duration_min, 0.0)) as total_burn_duration_min
from {{ ref('int_interval_measurements') }}
group by
    messpunkt_id,
    objekt_id,
    sparte,
    channel,
    unit,
    local_date,
    report_year,
    report_quarter,
    year_quarter
