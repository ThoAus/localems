with daily as (
    select * from {{ ref('int_metering_point_daily') }}
),

active_estimations as (
    select *
    from {{ ref('stg_estimation_rules') }}
    where is_active = true
),

-- Direct sub-meter deductions derived directly from the meter tree (arbitrary depth, zero double counting)
submeter_deductions as (
    select
        child.parent_metering_point_id as metering_point_id,
        child.energy_carrier,
        child.channel,
        child.local_date,
        count(*) as direct_child_count,
        sum(child.gross_value) as submeter_deduction_value
    from daily child
    where child.parent_metering_point_id is not null
    group by
        child.parent_metering_point_id,
        child.energy_carrier,
        child.channel,
        child.local_date
),

-- Total active estimation factor share allocated away from each meter on a given day
estimation_shares as (
    select
        d.metering_point_id,
        d.local_date,
        sum(r.factor) as estimation_factor_sum
    from daily d
    inner join active_estimations r
        on r.source_metering_point_id = d.metering_point_id
       and d.local_date between r.valid_from and r.valid_to_effective
    group by
        d.metering_point_id,
        d.local_date
),

combined as (
    select
        d.metering_point_id,
        d.parent_metering_point_id,
        d.root_metering_point_id,
        d.meter_level,
        d.is_main_meter,
        d.tree_path,
        d.object_id,
        d.energy_carrier,
        d.channel,
        d.unit,
        d.local_date,
        d.report_year,
        d.report_quarter,
        d.year_quarter,
        d.gross_value,
        coalesce(sub.direct_child_count, 0) as direct_child_count,
        coalesce(sub.submeter_deduction_value, 0.0) as submeter_deduction_value,
        d.gross_value - coalesce(sub.submeter_deduction_value, 0.0) as net_after_submeters,
        coalesce(est.estimation_factor_sum, 0.0) as estimation_factor_sum,
        (d.gross_value - coalesce(sub.submeter_deduction_value, 0.0))
            * (1.0 - coalesce(est.estimation_factor_sum, 0.0)) as residual_value,
        (d.gross_value - coalesce(sub.submeter_deduction_value, 0.0)) < -1e-6 as is_negative_residual,
        d.interval_count,
        d.negative_interval_count,
        d.max_peak_power_kw,
        d.avg_flow_rate,
        d.avg_pressure_bar,
        d.total_burn_duration_min
    from daily d
    left join submeter_deductions sub
        on d.metering_point_id = sub.metering_point_id
       and d.energy_carrier = sub.energy_carrier
       and d.channel = sub.channel
       and d.local_date = sub.local_date
    left join estimation_shares est
        on d.metering_point_id = est.metering_point_id
       and d.local_date = est.local_date
)

select * from combined
