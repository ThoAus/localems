select
    cast(rule_id as bigint) as rule_id,
    source_metering_point_id,
    target_object_id,
    cast(factor as double) as factor,
    cast(valid_from as date) as valid_from,
    coalesce(try_cast(valid_to as date), date '9999-12-31') as valid_to_effective,
    try_cast(valid_to as date) as valid_to,
    cast(active as boolean) as is_active,
    notes,
    updated_at
from masterdata.main.estimation_rules
