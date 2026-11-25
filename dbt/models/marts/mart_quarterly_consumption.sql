with allocated as (
    select * from {{ ref('int_object_daily_allocated') }}
),

objekte as (
    select * from {{ ref('stg_objekte') }}
)

select
    a.report_year,
    a.report_quarter,
    a.year_quarter,
    a.sparte,
    a.channel,
    o.ebene_1,
    o.ebene_2,
    o.objekt_typ,
    a.objekt_id,
    o.objekt_name,
    a.unit,
    a.allocation_type,
    sum(a.allocated_value) as total_consumption,
    max(a.max_peak_power_kw) as max_peak_power_kw,
    avg(a.avg_flow_rate) as avg_flow_rate,
    avg(a.avg_pressure_bar) as avg_pressure_bar,
    sum(coalesce(a.total_burn_duration_min, 0.0)) as total_burn_duration_min,
    count(distinct a.local_date) as active_days
from allocated a
left join objekte o
    on a.objekt_id = o.objekt_id
group by
    a.report_year,
    a.report_quarter,
    a.year_quarter,
    a.sparte,
    a.channel,
    o.ebene_1,
    o.ebene_2,
    o.objekt_typ,
    a.objekt_id,
    o.objekt_name,
    a.unit,
    a.allocation_type
order by
    a.report_year,
    a.report_quarter,
    a.sparte,
    o.ebene_1,
    o.ebene_2,
    a.objekt_id,
    a.allocation_type
