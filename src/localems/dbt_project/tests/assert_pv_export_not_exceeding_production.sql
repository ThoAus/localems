-- Fails if grid export exceeds PV production in any 15-minute interval at a self-consumption site.
select
    site_metering_point_id,
    timestamp_utc,
    pv_production_kwh,
    grid_export_kwh
from {{ ref('int_pv_interval_balance') }}
where is_export_exceeding_production = true
