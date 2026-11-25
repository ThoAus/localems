select
    object_id,
    name as object_name,
    lower(object_class) as object_class,
    level_1,
    level_2,
    cast(active as boolean) as is_active,
    notes,
    updated_at
from masterdata.main.objects
