select
    metering_point_id,
    parent_metering_point_id,
    object_id,
    lower(energy_carrier) as energy_carrier,
    name as metering_point_name,
    external_id,
    unit as master_unit,
    lower(meter_type) as meter_type,
    cast(active as boolean) as is_active,
    try_cast(valid_from as date) as valid_from,
    try_cast(valid_to as date) as valid_to,
    notes,
    updated_at
from masterdata.main.metering_points
