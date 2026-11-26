"""End-to-end integration test: CLI + Parquet + meter tree + PV plants + dbt-duckdb run & test."""

from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta
from pathlib import Path

import duckdb
import openpyxl
import polars as pl
import pytest

from localems import EnergyReporter, LocalEmsConfig, get_dbt_project_dir, run_dbt
from localems.cli import main as cli_main
from localems.storage import ParquetMeasurementStore

REPO_ROOT = Path(__file__).resolve().parents[1]


def _make_full_day_rows(
    day: datetime,
    metering_point_id: str,
    energy_carrier: str,
    unit: str,
    daily_total: float,
    *,
    channel: str = "consumption",
    peak_power_kw: float | None = None,
) -> list[dict]:
    per_interval = daily_total / 96.0
    rows = []
    for i in range(96):
        ts = day + timedelta(minutes=15 * i)
        rows.append(
            {
                "timestamp": ts,
                "interval_minutes": 15,
                "metering_point_id": metering_point_id,
                "energy_carrier": energy_carrier,
                "channel": channel,
                "value": per_interval,
                "unit": unit,
                "peak_power_kw": peak_power_kw,
                "source": "integration_test",
            }
        )
    return rows


def test_end_to_end_mixed_group_meter_tree_pv_and_dbt_marts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if importlib.util.find_spec("dbt.cli.main") is None:
        pytest.skip("dbt-duckdb is not installed in this environment")

    data_dir = tmp_path / "data"
    assert cli_main(["--data-dir", str(data_dir), "init"]) == 0
    assert cli_main(["--data-dir", str(data_dir), "export-editor"]) == 0

    editor_xlsx = data_dir / "editor" / "masterdata_roundtrip.xlsx"
    wb = openpyxl.load_workbook(editor_xlsx)

    # Mixed object groups under one bidirectional main electricity meter WITH behind-the-meter PV
    # + standalone full feed-in PV plant + independent water meter:
    #   MP-TOWN-MAIN (grid_import=750, grid_export=150) + attached MP-TOWN-PV (pv_production=400)
    #     -> self_consumption = 250 kWh (62.5% of PV), autarky = 25.0%
    #     -> total busbar supply = 750 + 400 - 150 = 1000.0 kWh
    #   ├── MP-BLDG   (500 kWh, L1, rest -> OBJ-TOWNHALL [Buildings / Administration] = 380)
    #   │   └── MP-LIB (120 kWh, L2, measured -> OBJ-LIBRARY [Buildings / Culture] = 120)
    #   ├── MP-SCHOOL (300 kWh, L1, measured -> OBJ-SCHOOL [Education / Primary Schools] = 300)
    #   └── MP-TOWN-PV (400 kWh, L1, pv_production feeding MP-TOWN-MAIN)
    #   estimation_rules: 20% of MP-TOWN-MAIN's net rest (200 kWh) -> OBJ-EV [Mobility] = 40 kWh
    #   automatic remaining rest -> OBJ-STREETLIGHTS [Public Lighting] = 160 kWh
    #   Total electricity consumption = 120 + 380 + 300 + 40 + 160 = 1000.0 kWh (conserved!)
    #   Standalone full feed-in PV: MP-PV-SOLO (600 kWh pv_production & grid_export) -> PV mart only
    ws_obj = wb["objects"]
    ws_obj.append(
        ["OBJ-STREETLIGHTS", "Center Streetlights", "Public Lighting", "Center", 1, None]
    )
    ws_obj.append(["OBJ-TOWNHALL", "Town Hall", "Buildings", "Administration", 1, None])
    ws_obj.append(["OBJ-LIBRARY", "Public Library", "Buildings", "Culture", 1, None])
    ws_obj.append(["OBJ-SCHOOL", "Primary School", "Education", "Primary Schools", 1, None])
    ws_obj.append(["OBJ-EV", "EV Charging Station", "Mobility", "Charging", 1, None])
    ws_obj.append(["OBJ-RESERVOIR", "North Reservoir", "Water Supply", "Reservoirs", 1, None])
    ws_obj.append(["OBJ-SOLAR-PARK", "Depot PV Roof", "Energy Generation", "PV Plants", 1, None])

    ws_mp = wb["metering_points"]
    ws_mp.append(
        [
            "MP-TOWN-MAIN", None, "OBJ-STREETLIGHTS", "electricity", "Main Town Grid Meter",
            "POD-MAIN", "kWh", "smartmeter", 1, "2026-01-01", None, None,
        ]
    )
    ws_mp.append(
        [
            "MP-TOWN-PV", "MP-TOWN-MAIN", "OBJ-STREETLIGHTS", "electricity", "Town Hall PV Array",
            "M2-TOWN", "kWh", "pv_production", 1, "2026-01-01", None, None,
        ]
    )
    ws_mp.append(
        [
            "MP-BLDG", "MP-TOWN-MAIN", "OBJ-TOWNHALL", "electricity", "Town Hall Sub-meter",
            "SUB-HALL", "kWh", "submeter", 1, "2026-01-01", None, None,
        ]
    )
    ws_mp.append(
        [
            "MP-LIB", "MP-BLDG", "OBJ-LIBRARY", "electricity", "Library Sub-sub-meter",
            "SUB-LIB", "kWh", "submeter", 1, "2026-01-01", None, None,
        ]
    )
    ws_mp.append(
        [
            "MP-SCHOOL", "MP-TOWN-MAIN", "OBJ-SCHOOL", "electricity", "School Sub-meter",
            "SUB-SCH", "kWh", "submeter", 1, "2026-01-01", None, None,
        ]
    )
    ws_mp.append(
        [
            "MP-PV-SOLO", None, "OBJ-SOLAR-PARK", "electricity", "Depot Full Feed-In PV",
            "M2-DEPOT", "kWh", "pv_production", 1, "2026-01-01", None, None,
        ]
    )
    ws_mp.append(
        [
            "MP-WATER-1", None, "OBJ-RESERVOIR", "water", "Reservoir Water Meter",
            "W-1", "m3", "smartmeter", 1, "2026-01-01", None, None,
        ]
    )

    ws_est = wb["estimation_rules"]
    ws_est.append([None, "MP-TOWN-MAIN", "OBJ-EV", 0.20, "2026-01-01", None, 1, "20% of rest"])
    wb.save(editor_xlsx)
    wb.close()

    assert cli_main(["--data-dir", str(data_dir), "import-editor", str(editor_xlsx)]) == 0

    # Write 1 full 15-min day (2026-02-10 local Europe/Rome) for all meters and channels
    day = datetime(2026, 2, 10, 0, 0)
    rows = []
    # Bidirectional main meter: grid_import=750, grid_export=150
    rows.extend(
        _make_full_day_rows(
            day, "MP-TOWN-MAIN", "electricity", "kWh", 750.0,
            channel="grid_import", peak_power_kw=48.0,
        )
    )
    rows.extend(
        _make_full_day_rows(
            day, "MP-TOWN-MAIN", "electricity", "kWh", 150.0, channel="grid_export"
        )
    )
    # Attached behind-the-meter PV: pv_production=400
    rows.extend(
        _make_full_day_rows(
            day, "MP-TOWN-PV", "electricity", "kWh", 400.0, channel="pv_production"
        )
    )
    # Consumer sub-meters (notice 500 + 300 = 800 > grid_import 750, valid because PV adds 250!)
    rows.extend(
        _make_full_day_rows(day, "MP-BLDG", "electricity", "kWh", 500.0, peak_power_kw=28.0)
    )
    rows.extend(
        _make_full_day_rows(day, "MP-LIB", "electricity", "kWh", 120.0, peak_power_kw=9.0)
    )
    rows.extend(
        _make_full_day_rows(day, "MP-SCHOOL", "electricity", "kWh", 300.0, peak_power_kw=18.0)
    )
    # Standalone full feed-in PV plant
    rows.extend(
        _make_full_day_rows(
            day, "MP-PV-SOLO", "electricity", "kWh", 600.0, channel="pv_production"
        )
    )
    rows.extend(
        _make_full_day_rows(
            day, "MP-PV-SOLO", "electricity", "kWh", 600.0, channel="grid_export"
        )
    )
    # Water meter
    rows.extend(_make_full_day_rows(day, "MP-WATER-1", "water", "m3", 48.0))

    store = ParquetMeasurementStore(data_dir / "parquet")
    store.write(pl.DataFrame(rows))

    # Run packaged dbt run & dbt test from an unrelated working directory
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LOCALEMS_DUCKDB_EXT_DIR", (REPO_ROOT / ".duckdb_extensions").as_posix())

    cfg = LocalEmsConfig.from_env(data_dir)
    duckdb_path = cfg.duckdb_path

    dbt_dir = get_dbt_project_dir()
    assert (dbt_dir / "dbt_project.yml").is_file()

    run_res = run_dbt("run", config=cfg, capture_output=True)
    assert run_res.ok, f"dbt run failed:\n{run_res.stdout}\n{run_res.stderr}"

    test_res = run_dbt("test", config=cfg, capture_output=True)
    assert test_res.ok, f"dbt test failed:\n{test_res.stdout}\n{test_res.stderr}"

    # Verify CLI wrapper works identically and package dbt_project stays clean (read-only safe)
    assert cli_main(["--data-dir", str(data_dir), "dbt", "test"]) == 0
    assert (cfg.dbt_target_dir / "manifest.json").is_file()
    assert not (dbt_dir / "target").exists()
    assert not (dbt_dir / "logs").exists()
    assert not (dbt_dir / ".user.yml").exists()

    # Verify daily materialized tables + monthly convenience views directly (no ATTACH needed!)
    with duckdb.connect(str(duckdb_path), read_only=True) as con:
        m_rows = con.execute(
            """
            select
                object_id,
                energy_carrier,
                "group",
                sub_group,
                allocation_type,
                round(total_consumption, 4) as total_consumption
            from mart_monthly_consumption
            where year_month = '2026-02'
            order by energy_carrier, object_id
            """
        ).fetchall()

        pv_rows = con.execute(
            """
            select
                site_metering_point_id,
                plant_mode,
                pv_production_kwh,
                grid_export_kwh,
                grid_import_kwh,
                self_consumption_kwh,
                total_site_consumption_kwh,
                self_consumption_rate_pct,
                autarky_rate_pct
            from mart_monthly_pv_summary
            where year_month = '2026-02'
            order by site_metering_point_id
            """
        ).fetchall()

        tree_rows = con.execute(
            """
            select metering_point_id, meter_level, is_main_meter, tree_path
            from mart_plausibility_summary
            where year_month = '2026-02'
            order by tree_path
            """
        ).fetchall()

    results = {(r[0], r[1]): (r[2], r[3], r[4], r[5]) for r in m_rows}
    # Standalone PV generator must NOT appear as a consumer
    assert ("OBJ-SOLAR-PARK", "electricity") not in results
    assert results[("OBJ-STREETLIGHTS", "electricity")] == (
        "Public Lighting", "Center", "residual", pytest.approx(160.0)
    )
    assert results[("OBJ-EV", "electricity")] == (
        "Mobility", "Charging", "estimated_share", pytest.approx(40.0)
    )
    assert results[("OBJ-TOWNHALL", "electricity")] == (
        "Buildings", "Administration", "residual", pytest.approx(380.0)
    )
    assert results[("OBJ-LIBRARY", "electricity")] == (
        "Buildings", "Culture", "measured", pytest.approx(120.0)
    )
    assert results[("OBJ-SCHOOL", "electricity")] == (
        "Education", "Primary Schools", "measured", pytest.approx(300.0)
    )
    assert results[("OBJ-RESERVOIR", "water")] == (
        "Water Supply", "Reservoirs", "measured", pytest.approx(48.0)
    )

    # Energy conservation: total allocated electricity == 750 (import) + 400 (PV) - 150 (export)
    electricity_total = sum(
        val for (_, carrier), (_, _, _, val) in results.items() if carrier == "electricity"
    )
    assert electricity_total == pytest.approx(1000.0)

    # Verify PV KPIs for both behind-the-meter and standalone full feed-in plants
    pv_map = {r[0]: r[1:] for r in pv_rows}
    assert pv_map["MP-TOWN-MAIN"] == (
        "self_consumption",
        pytest.approx(400.0),
        pytest.approx(150.0),
        pytest.approx(750.0),
        pytest.approx(250.0),
        pytest.approx(1000.0),
        pytest.approx(62.5),
        pytest.approx(25.0),
    )
    assert pv_map["MP-PV-SOLO"] == (
        "full_feed_in",
        pytest.approx(600.0),
        pytest.approx(600.0),
        pytest.approx(0.0),
        pytest.approx(0.0),
        pytest.approx(0.0),
        pytest.approx(0.0),
        None,
    )

    # Verify automatic recursive tree depth & paths
    tree_map = {r[0]: (r[1], r[2], r[3]) for r in tree_rows}
    assert tree_map["MP-TOWN-MAIN"] == (0, True, "MP-TOWN-MAIN")
    assert tree_map["MP-BLDG"] == (1, False, "MP-TOWN-MAIN > MP-BLDG")
    assert tree_map["MP-LIB"] == (2, False, "MP-TOWN-MAIN > MP-BLDG > MP-LIB")
    assert tree_map["MP-SCHOOL"] == (1, False, "MP-TOWN-MAIN > MP-SCHOOL")
    assert tree_map["MP-WATER-1"] == (0, True, "MP-WATER-1")

    # Verify standardized EnergyReporter API (grouping, single group/sub_group, pivot, PV, raw)
    rep = EnergyReporter(cfg)

    by_group = rep.consumption(period="month", group_by="group", energy_carrier="electricity")
    grp_totals = dict(
        zip(by_group["group"].to_list(), by_group["total_consumption"].to_list(), strict=True)
    )
    assert grp_totals["Buildings"] == pytest.approx(500.0)  # 380 Town Hall + 120 Library
    assert grp_totals["Education"] == pytest.approx(300.0)
    assert grp_totals["Public Lighting"] == pytest.approx(160.0)
    assert grp_totals["Mobility"] == pytest.approx(40.0)

    single_sub = rep.consumption(
        period="quarter",
        group="Buildings",
        sub_group="Culture",
        year=2026,
    )
    assert single_sub.height == 1
    assert single_sub["object_id"].to_list() == ["OBJ-LIBRARY"]
    assert single_sub["total_consumption"].to_list() == [pytest.approx(120.0)]

    pivoted = rep.consumption(
        period="quarter",
        group_by=["group", "sub_group"],
        energy_carrier="electricity",
        pivot=True,
    )
    assert "2026-Q1" in pivoted.columns
    assert "total" in pivoted.columns
    assert pivoted["total"].sum() == pytest.approx(1000.0)

    pv_q = rep.pv_summary(period="quarter", group="Public Lighting")
    assert pv_q.height == 1
    assert pv_q["self_consumption_rate_pct"].to_list() == [pytest.approx(62.5)]
    assert pv_q["autarky_rate_pct"].to_list() == [pytest.approx(25.0)]

    plaus = rep.plausibility(metering_point_id="MP-LIB")
    assert plaus.height == 1
    assert plaus["meter_level"].to_list() == [2]

    raw_intervals = rep.intervals(
        metering_point_ids=["MP-BLDG", "MP-LIB"],
        energy_carrier="electricity",
        channel="consumption",
        year=2026,
    )
    assert raw_intervals.height == 2 * 96
    assert "timestamp_local" in raw_intervals.columns

    groups_df = rep.groups()
    assert groups_df.height == 7

    # Verify CLI `localems report` subcommands & .xlsx / .csv exports
    xlsx_out = tmp_path / "buildings_report.xlsx"
    csv_out = tmp_path / "pv_report.csv"
    assert (
        cli_main(
            [
                "--data-dir",
                str(data_dir),
                "report",
                "consumption",
                "--period",
                "month",
                "--group",
                "Buildings",
                "--group-by",
                "sub_group",
                "--pivot",
                "-o",
                str(xlsx_out),
            ]
        )
        == 0
    )
    assert xlsx_out.is_file()
    wb_out = openpyxl.load_workbook(xlsx_out)
    assert wb_out["consumption"].max_row == 3  # header + Administration + Culture
    wb_out.close()

    assert (
        cli_main(
            [
                "--data-dir",
                str(data_dir),
                "report",
                "pv",
                "--period",
                "month",
                "-o",
                str(csv_out),
            ]
        )
        == 0
    )
    assert csv_out.is_file()
    assert (
        cli_main(
            [
                "--data-dir",
                str(data_dir),
                "report",
                "intervals",
                "--meter",
                "MP-BLDG,MP-LIB",
                "--limit",
                "5",
            ]
        )
        == 0
    )
    assert cli_main(["--data-dir", str(data_dir), "report", "groups"]) == 0
