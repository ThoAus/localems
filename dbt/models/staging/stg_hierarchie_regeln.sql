select
    cast(rule_id as bigint) as rule_id,
    parent_messpunkt_id,
    child_messpunkt_id,
    target_objekt_id,
    lower(rule_type) as rule_type,
    cast(factor as double) as factor,
    cast(valid_from as date) as valid_from,
    coalesce(try_cast(valid_to as date), date '9999-12-31') as valid_to_effective,
    try_cast(valid_to as date) as valid_to,
    cast(active as boolean) as is_active,
    notes,
    updated_at
from masterdata.main.hierarchie_regeln
