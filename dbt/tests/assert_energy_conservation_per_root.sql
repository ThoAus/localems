-- Energy conservation test: for every main (root) meter and day, the sum of all allocated
-- shares across its entire subtree (measured + residual + estimated_share) must equal
-- the root meter's gross measured consumption (zero double counting, zero lost energy).
with root_totals as (
    select
        root_metering_point_id,
        energy_carrier,
        channel,
        local_date,
        gross_value as root_gross_value
    from {{ ref('int_metering_point_daily') }}
    where is_main_meter = true
),

allocated_totals as (
    select
        root_metering_point_id,
        energy_carrier,
        channel,
        local_date,
        sum(allocated_value) as total_allocated_value
    from {{ ref('int_object_daily_allocated') }}
    group by 1, 2, 3, 4
)

select
    r.root_metering_point_id,
    r.energy_carrier,
    r.channel,
    r.local_date,
    r.root_gross_value,
    a.total_allocated_value,
    abs(r.root_gross_value - a.total_allocated_value) as discrepancy
from root_totals r
inner join allocated_totals a
    on r.root_metering_point_id = a.root_metering_point_id
   and r.energy_carrier = a.energy_carrier
   and r.channel = a.channel
   and r.local_date = a.local_date
where abs(r.root_gross_value - a.total_allocated_value) > 1e-4
