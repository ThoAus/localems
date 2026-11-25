with daily as (
    select * from {{ ref('int_messpunkt_daily') }}
),

active_rules as (
    select *
    from {{ ref('stg_hierarchie_regeln') }}
    where is_active = true
),

-- Direct sub-meter deductions per parent meter and day (multi-level safe, no double counting)
submeter_deductions as (
    select
        r.parent_messpunkt_id as messpunkt_id,
        child.sparte,
        child.channel,
        child.local_date,
        sum(child.gross_value * r.factor) as submeter_deduction_value
    from active_rules r
    inner join daily child
        on child.messpunkt_id = r.child_messpunkt_id
       and child.local_date between r.valid_from and r.valid_to_effective
    where r.rule_type = 'submeter_deduction'
    group by
        r.parent_messpunkt_id,
        child.sparte,
        child.channel,
        child.local_date
),

-- Total estimation factor share allocated away from each parent meter on a given day
estimation_shares as (
    select
        d.messpunkt_id,
        d.local_date,
        sum(r.factor) as estimation_factor_sum
    from daily d
    inner join active_rules r
        on r.parent_messpunkt_id = d.messpunkt_id
       and r.rule_type = 'estimation_factor'
       and d.local_date between r.valid_from and r.valid_to_effective
    group by
        d.messpunkt_id,
        d.local_date
),

combined as (
    select
        d.messpunkt_id,
        d.objekt_id,
        d.sparte,
        d.channel,
        d.unit,
        d.local_date,
        d.report_year,
        d.report_quarter,
        d.year_quarter,
        d.gross_value,
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
        on d.messpunkt_id = sub.messpunkt_id
       and d.sparte = sub.sparte
       and d.channel = sub.channel
       and d.local_date = sub.local_date
    left join estimation_shares est
        on d.messpunkt_id = est.messpunkt_id
       and d.local_date = est.local_date
)

select * from combined
