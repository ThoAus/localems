-- Fails if duplicate natural keys exist in Parquet measurements
select
    metering_point_id,
    energy_carrier,
    channel,
    timestamp_utc,
    reading_type,
    count(*) as row_count
from {{ ref('stg_measurements') }}
group by 1, 2, 3, 4, 5
having count(*) > 1
