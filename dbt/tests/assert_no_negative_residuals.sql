-- Fails if sub-meter deductions exceed parent meter consumption on any day
select
    messpunkt_id,
    sparte,
    channel,
    local_date,
    gross_value,
    submeter_deduction_value,
    net_after_submeters
from {{ ref('int_hierarchy_residuals') }}
where is_negative_residual = true
