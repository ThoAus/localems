-- Multi-level busbar energy balance & automatic residuals:
--   gross_value (total site supply) = import_value + attached_pv_production - export_value
--   submeter_deduction              = sum(child.import_value - child.export_value) for consumer children
--   net_after_submeters             = gross_value - submeter_deduction
--   residual_value                  = net_after_submeters * (1 - estimation_factor_sum)
with daily as (
    select * from {{ ref('int_metering_point_daily') }}
),

consumer_daily as (
    select *
    from daily
    where meter_type <> 'pv_production'
),

active_estimations as (
    select *
    from {{ ref('stg_estimation_rules') }}
    where is_active = true
),

-- Behind-the-meter PV production children feeding into their parent meter
attached_pv as (
    select
        child.parent_metering_point_id as metering_point_id,
        child.energy_carrier,
        child.local_date,
        sum(child.pv_production_value) as attached_pv_production_value
    from daily child
    where child.parent_metering_point_id is not null
      and child.meter_type = 'pv_production'
    group by
        child.parent_metering_point_id,
        child.energy_carrier,
        child.local_date
),

-- Direct consumer sub-meter deductions (net flow crossing the wire: import - export)
submeter_deductions as (
    select
        child.parent_metering_point_id as metering_point_id,
        child.energy_carrier,
        child.local_date,
        count(*) as direct_child_count,
        sum(child.import_value - child.export_value) as submeter_deduction_value
    from consumer_daily child
    where child.parent_metering_point_id is not null
    group by
        child.parent_metering_point_id,
        child.energy_carrier,
        child.local_date
),

-- Total active estimation factor share allocated away from each meter on a given day
estimation_shares as (
    select
        d.metering_point_id,
        d.local_date,
        sum(r.factor) as estimation_factor_sum
    from consumer_daily d
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
        d.meter_type,
        d.object_id,
        d.energy_carrier,
        d.unit,
        d.local_date,
        d.report_year,
        d.report_quarter,
        d.year_quarter,
        d.import_value as grid_import_value,
        d.export_value as grid_export_value,
        coalesce(pv.attached_pv_production_value, 0.0) as attached_pv_production_value,
        case
            when coalesce(pv.attached_pv_production_value, 0.0) > 0 or d.export_value > 0
                then coalesce(pv.attached_pv_production_value, 0.0) - d.export_value
            else 0.0
        end as self_consumption_value,
        d.import_value + coalesce(pv.attached_pv_production_value, 0.0) - d.export_value as gross_value,
        coalesce(sub.direct_child_count, 0) as direct_child_count,
        coalesce(sub.submeter_deduction_value, 0.0) as submeter_deduction_value,
        (d.import_value + coalesce(pv.attached_pv_production_value, 0.0) - d.export_value)
            - coalesce(sub.submeter_deduction_value, 0.0) as net_after_submeters,
        coalesce(est.estimation_factor_sum, 0.0) as estimation_factor_sum,
        (
            (d.import_value + coalesce(pv.attached_pv_production_value, 0.0) - d.export_value)
            - coalesce(sub.submeter_deduction_value, 0.0)
        ) * (1.0 - coalesce(est.estimation_factor_sum, 0.0)) as residual_value,
        (
            (d.import_value + coalesce(pv.attached_pv_production_value, 0.0) - d.export_value)
            - coalesce(sub.submeter_deduction_value, 0.0)
        ) < -1e-6 as is_negative_residual,
        d.interval_count,
        d.negative_interval_count,
        d.max_peak_power_kw,
        d.avg_flow_rate,
        d.avg_pressure_bar,
        d.total_burn_duration_min
    from consumer_daily d
    left join attached_pv pv
        on d.metering_point_id = pv.metering_point_id
       and d.energy_carrier = pv.energy_carrier
       and d.local_date = pv.local_date
    left join submeter_deductions sub
        on d.metering_point_id = sub.metering_point_id
       and d.energy_carrier = sub.energy_carrier
       and d.local_date = sub.local_date
    left join estimation_shares est
        on d.metering_point_id = est.metering_point_id
       and d.local_date = est.local_date
)

select * from combined
