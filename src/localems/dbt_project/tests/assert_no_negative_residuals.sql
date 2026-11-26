-- Fails if sub-meter deductions exceed parent meter busbar supply on any day
select
    metering_point_id,
    energy_carrier,
    local_date,
    gross_value,
    submeter_deduction_value,
    net_after_submeters
from {{ ref('int_hierarchy_residuals') }}
where is_negative_residual = true
