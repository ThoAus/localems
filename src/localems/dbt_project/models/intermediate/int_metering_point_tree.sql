-- Recursively derives meter_level (0 = main meter, 1..N = sub-meter depth),
-- root_metering_point_id, and full tree_path with no arbitrary depth limit.
with recursive meter_tree as (
    select
        mp.metering_point_id,
        mp.parent_metering_point_id,
        mp.object_id,
        mp.energy_carrier,
        mp.metering_point_name,
        mp.external_id,
        mp.master_unit,
        mp.meter_type,
        mp.is_active,
        0 as meter_level,
        true as is_main_meter,
        mp.metering_point_id as root_metering_point_id,
        mp.metering_point_id as tree_path
    from {{ ref('stg_metering_points') }} mp
    where mp.parent_metering_point_id is null

    union all

    select
        child.metering_point_id,
        child.parent_metering_point_id,
        child.object_id,
        child.energy_carrier,
        child.metering_point_name,
        child.external_id,
        child.master_unit,
        child.meter_type,
        child.is_active,
        parent.meter_level + 1 as meter_level,
        false as is_main_meter,
        parent.root_metering_point_id,
        concat(parent.tree_path, ' > ', child.metering_point_id) as tree_path
    from {{ ref('stg_metering_points') }} child
    inner join meter_tree parent
        on child.parent_metering_point_id = parent.metering_point_id
)

select * from meter_tree
