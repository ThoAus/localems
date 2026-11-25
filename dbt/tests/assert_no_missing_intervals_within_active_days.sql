-- Fails if any active 15-minute day is missing intervals.
-- Allows 96 (normal day), 92 (EU spring-forward DST 23h day), and 100 (EU fall-back DST 25h day).
with daily_15m as (
    select
        messpunkt_id,
        sparte,
        channel,
        local_date,
        count(*) as interval_count
    from {{ ref('int_interval_measurements') }}
    where interval_minutes = 15
    group by 1, 2, 3, 4
)

select *
from daily_15m
where interval_count not in (92, 96, 100)
