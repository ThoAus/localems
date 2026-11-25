-- Fails if any interval measurement has negative consumption
select
    messpunkt_id,
    sparte,
    channel,
    timestamp_utc,
    value
from {{ ref('stg_measurements') }}
where reading_type = 'interval'
  and value < 0
