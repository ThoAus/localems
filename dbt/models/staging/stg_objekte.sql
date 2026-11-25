select
    objekt_id,
    name as objekt_name,
    lower(objekt_typ) as objekt_typ,
    ebene_1,
    ebene_2,
    cast(active as boolean) as is_active,
    notes,
    updated_at
from masterdata.main.objekte
