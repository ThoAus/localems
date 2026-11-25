-- Energy conservation test: for every non-PV root meter and day, the sum of all allocated
-- consumptions across its entire subtree (measured + residual + estimated_share) must equal
-- root_grid_import - root_grid_export + sum(all pv_production inside the subtree).
with root_net_grid as (
    select
        root_metering_point_id,
        energy_carrier,
        local_date,
        import_value - export_value as root_net_grid_supply
    from {{ ref('int_metering_point_daily') }}
    where is_main_meter = true
      and meter_type <> 'pv_production'
),

subtree_pv as (
    select
        root_metering_point_id,
        energy_carrier,
        local_date,
        sum(pv_production_value) as subtree_pv_production
    from {{ ref('int_metering_point_daily') }}
    where meter_type = 'pv_production'
    group by 1, 2, 3
),

expected_supply as (
    select
        r.root_metering_point_id,
        r.energy_carrier,
        r.local_date,
        r.root_net_grid_supply + coalesce(pv.subtree_pv_production, 0.0) as expected_total_consumption
    from root_net_grid r
    left join subtree_pv pv
        on r.root_metering_point_id = pv.root_metering_point_id
       and r.energy_carrier = pv.energy_carrier
       and r.local_date = pv.local_date
),

allocated_totals as (
    select
        root_metering_point_id,
        energy_carrier,
        local_date,
        sum(allocated_value) as total_allocated_value
    from {{ ref('int_object_daily_allocated') }}
    group by 1, 2, 3
)

select
    e.root_metering_point_id,
    e.energy_carrier,
    e.local_date,
    e.expected_total_consumption,
    a.total_allocated_value,
    abs(e.expected_total_consumption - a.total_allocated_value) as discrepancy
from expected_supply e
inner join allocated_totals a
    on e.root_metering_point_id = a.root_metering_point_id
   and e.energy_carrier = a.energy_carrier
   and e.local_date = a.local_date
where abs(e.expected_total_consumption - a.total_allocated_value) > 1e-4
