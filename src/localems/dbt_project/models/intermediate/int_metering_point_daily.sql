-- Daily metering point rollups pivoting electricity flow channels (import, export, pv_production).
with daily as (
    select
        metering_point_id,
        parent_metering_point_id,
        root_metering_point_id,
        meter_level,
        is_main_meter,
        tree_path,
        meter_type,
        object_id,
        energy_carrier,
        unit,
        local_date,
        report_year,
        report_quarter,
        year_quarter,
        sum(case when channel in ('consumption', 'grid_import') then value else 0.0 end) as import_value,
        sum(case when channel = 'grid_export' then value else 0.0 end) as export_value,
        sum(case when channel = 'pv_production' then value else 0.0 end) as pv_production_value,
        sum(case when channel in ('consumption', 'grid_import') then 1 else 0 end) as import_interval_count,
        sum(case when channel = 'grid_export' then 1 else 0 end) as export_interval_count,
        sum(case when channel = 'pv_production' then 1 else 0 end) as pv_interval_count,
        sum(case when value < 0 then 1 else 0 end) as negative_interval_count,
        max(peak_power_kw) as max_peak_power_kw,
        avg(flow_rate) as avg_flow_rate,
        avg(pressure_bar) as avg_pressure_bar,
        sum(coalesce(burn_duration_min, 0.0)) as total_burn_duration_min
    from {{ ref('int_interval_measurements') }}
    group by
        metering_point_id,
        parent_metering_point_id,
        root_metering_point_id,
        meter_level,
        is_main_meter,
        tree_path,
        meter_type,
        object_id,
        energy_carrier,
        unit,
        local_date,
        report_year,
        report_quarter,
        year_quarter
)

select
    *,
    case
        when meter_type = 'pv_production' then pv_interval_count
        when import_interval_count > 0 then import_interval_count
        else export_interval_count
    end as interval_count
from daily
