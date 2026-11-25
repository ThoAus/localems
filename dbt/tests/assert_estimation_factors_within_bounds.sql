-- Fails if active estimation factors on any meter exceed 1.0 on any day
select
    metering_point_id,
    local_date,
    estimation_factor_sum
from {{ ref('int_hierarchy_residuals') }}
where estimation_factor_sum > 1.0 + 1e-9
