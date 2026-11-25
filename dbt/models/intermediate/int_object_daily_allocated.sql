with residuals as (
    select * from {{ ref('int_hierarchy_residuals') }}
),

estimation_rules as (
    select *
    from {{ ref('stg_estimation_rules') }}
    where is_active = true
),

-- Leaf meters emit 'measured'; parent meters (or meters with estimation splits) automatically emit 'residual'
direct_or_residual as (
    select
        local_date,
        report_year,
        report_quarter,
        year_quarter,
        energy_carrier,
        channel,
        unit,
        object_id,
        metering_point_id as source_metering_point_id,
        root_metering_point_id,
        case
            when direct_child_count > 0 or estimation_factor_sum > 0 then 'residual'
            else 'measured'
        end as allocation_type,
        residual_value as allocated_value,
        interval_count,
        max_peak_power_kw,
        avg_flow_rate,
        avg_pressure_bar,
        total_burn_duration_min
    from residuals
),

estimated_allocations as (
    select
        res.local_date,
        res.report_year,
        res.report_quarter,
        res.year_quarter,
        res.energy_carrier,
        res.channel,
        res.unit,
        r.target_object_id as object_id,
        res.metering_point_id as source_metering_point_id,
        res.root_metering_point_id,
        'estimated_share' as allocation_type,
        res.net_after_submeters * r.factor as allocated_value,
        res.interval_count,
        cast(null as double) as max_peak_power_kw,
        cast(null as double) as avg_flow_rate,
        cast(null as double) as avg_pressure_bar,
        cast(null as double) as total_burn_duration_min
    from residuals res
    inner join estimation_rules r
        on r.source_metering_point_id = res.metering_point_id
       and res.local_date between r.valid_from and r.valid_to_effective
)

select * from direct_or_residual
union all
select * from estimated_allocations
