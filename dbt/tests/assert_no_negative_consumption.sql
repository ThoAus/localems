-- Fails if any interval measurement has negative consumption
select
    metering_point_id,
    energy_carrier,
    channel,
    timestamp_utc,
    value
from {{ ref('stg_measurements') }}
where reading_type = 'interval'
  and value < 0
