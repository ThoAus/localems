select
    object_id,
    name as object_name,
    "group",
    sub_group,
    cast(active as boolean) as is_active,
    notes,
    updated_at
from masterdata.main.objects
