-- 15-minute interval PV energy balance per PV-equipped site (self-consumption or full feed-in).
with intervals as (
    select *
    from {{ ref('int_interval_measurements') }}
    where energy_carrier = 'electricity'
),

meter_tree as (
    select * from {{ ref('int_metering_point_tree') }}
),

-- Sites that have behind-the-meter PV production children (or standalone full feed-in PV roots)
pv_sites as (
    select distinct
        parent_metering_point_id as site_metering_point_id,
        'self_consumption' as plant_mode
    from meter_tree
    where meter_type = 'pv_production'
      and parent_metering_point_id is not null

    union

    select distinct
        metering_point_id as site_metering_point_id,
        'full_feed_in' as plant_mode
    from meter_tree
    where meter_type = 'pv_production'
      and parent_metering_point_id is null
),

-- Map each relevant interval row to its PV site
site_flows as (
    -- Self-consumption: grid import & grid export on the site grid meter
    select
        s.site_metering_point_id,
        s.plant_mode,
        i.timestamp_utc,
        i.local_date,
        i.report_year,
        i.report_quarter,
        i.year_quarter,
        i.unit,
        case when i.channel in ('consumption', 'grid_import') then i.value else 0.0 end as grid_import_kwh,
        case when i.channel = 'grid_export' then i.value else 0.0 end as grid_export_kwh,
        0.0 as pv_production_kwh
    from pv_sites s
    inner join intervals i
        on i.metering_point_id = s.site_metering_point_id
    where s.plant_mode = 'self_consumption'

    union all

    -- Self-consumption: attached pv_production child meters
    select
        s.site_metering_point_id,
        s.plant_mode,
        i.timestamp_utc,
        i.local_date,
        i.report_year,
        i.report_quarter,
        i.year_quarter,
        i.unit,
        0.0 as grid_import_kwh,
        0.0 as grid_export_kwh,
        case when i.channel = 'pv_production' then i.value else 0.0 end as pv_production_kwh
    from pv_sites s
    inner join intervals i
        on i.parent_metering_point_id = s.site_metering_point_id
       and i.meter_type = 'pv_production'
    where s.plant_mode = 'self_consumption'

    union all

    -- Standalone full feed-in PV plant (parent_metering_point_id IS NULL)
    select
        s.site_metering_point_id,
        s.plant_mode,
        i.timestamp_utc,
        i.local_date,
        i.report_year,
        i.report_quarter,
        i.year_quarter,
        i.unit,
        0.0 as grid_import_kwh,
        case when i.channel = 'grid_export' then i.value else 0.0 end as grid_export_kwh,
        case when i.channel = 'pv_production' then i.value else 0.0 end as pv_production_kwh
    from pv_sites s
    inner join intervals i
        on i.metering_point_id = s.site_metering_point_id
    where s.plant_mode = 'full_feed_in'
),

aggregated as (
    select
        site_metering_point_id,
        plant_mode,
        timestamp_utc,
        local_date,
        report_year,
        report_quarter,
        year_quarter,
        unit,
        sum(grid_import_kwh) as grid_import_kwh,
        sum(grid_export_kwh) as grid_export_kwh,
        sum(pv_production_kwh) as pv_production_kwh
    from site_flows
    group by 1, 2, 3, 4, 5, 6, 7, 8
)

select
    a.site_metering_point_id,
    a.plant_mode,
    mt.object_id,
    a.timestamp_utc,
    a.local_date,
    a.report_year,
    a.report_quarter,
    a.year_quarter,
    a.unit,
    a.grid_import_kwh,
    a.grid_export_kwh,
    a.pv_production_kwh,
    case
        when a.plant_mode = 'self_consumption' then a.pv_production_kwh - a.grid_export_kwh
        else 0.0
    end as self_consumption_kwh,
    case
        when a.plant_mode = 'self_consumption'
            then a.grid_import_kwh + (a.pv_production_kwh - a.grid_export_kwh)
        else 0.0
    end as total_site_consumption_kwh,
    case
        when a.plant_mode = 'self_consumption'
            then (a.grid_export_kwh > a.pv_production_kwh + 1e-3)
        else false
    end as is_export_exceeding_production
from aggregated a
left join meter_tree mt
    on a.site_metering_point_id = mt.metering_point_id
