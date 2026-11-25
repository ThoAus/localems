"""End-to-end integration test: CLI + Parquet + meter tree roundtrip + dbt-duckdb run & test."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import duckdb
import openpyxl
import polars as pl
import pytest

from em_tools.cli import main as cli_main
from em_tools.storage import ParquetMeasurementStore

REPO_ROOT = Path(__file__).resolve().parents[1]
DBT_DIR = REPO_ROOT / "dbt"


def _make_full_day_rows(
    day: datetime,
    metering_point_id: str,
    energy_carrier: str,
    unit: str,
    daily_total: float,
    *,
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
                "value": per_interval,
                "unit": unit,
                "peak_power_kw": peak_power_kw,
                "source": "integration_test",
            }
        )
    return rows


def test_end_to_end_mixed_class_meter_tree_and_dbt_marts(tmp_path: Path) -> None:
    if importlib.util.find_spec("dbt.cli.main") is None:
        pytest.skip("dbt-duckdb is not installed in this environment")

    data_dir = tmp_path / "data"
    assert cli_main(["--data-dir", str(data_dir), "init"]) == 0
    assert cli_main(["--data-dir", str(data_dir), "export-editor"]) == 0

    editor_xlsx = data_dir / "editor" / "masterdata_roundtrip.xlsx"
    wb = openpyxl.load_workbook(editor_xlsx)

    # Mixed object classes under one main electricity meter + independent water meter:
    #   MP-TOWN-MAIN (1000 kWh, L0, rest -> OBJ-STREETLIGHTS [public_lighting])
    #   ├── MP-BLDG   (500 kWh, L1, rest -> OBJ-TOWNHALL [building])
    #   │   └── MP-LIB (120 kWh, L2, measured -> OBJ-LIBRARY [building])
    #   └── MP-SCHOOL (300 kWh, L1, measured -> OBJ-SCHOOL [school])
    #   estimation_rules: 20% of MP-TOWN-MAIN's net rest (200 kWh) -> OBJ-EV [mobility] (40 kWh)
    #   automatic remaining rest -> OBJ-STREETLIGHTS [public_lighting] (160 kWh)
    #   Total = 120 + 380 + 300 + 40 + 160 = 1000.0 kWh (exact conservation!)
    ws_obj = wb["objects"]
    ws_obj.append(
        ["OBJ-STREETLIGHTS", "Center Streetlights", "public_lighting", "Infra", "Lighting", 1, None]
    )
    ws_obj.append(["OBJ-TOWNHALL", "Town Hall", "building", "Admin", "Core", 1, None])
    ws_obj.append(["OBJ-LIBRARY", "Public Library", "building", "Culture", "Libraries", 1, None])
    ws_obj.append(["OBJ-SCHOOL", "Primary School", "school", "Education", "Schools", 1, None])
    ws_obj.append(["OBJ-EV", "EV Charging Station", "mobility", "Infra", "Charging", 1, None])
    ws_obj.append(
        ["OBJ-RESERVOIR", "North Reservoir", "water_supply", "Infra", "Water", 1, None]
    )

    ws_mp = wb["metering_points"]
    ws_mp.append(
        [
            "MP-TOWN-MAIN", None, "OBJ-STREETLIGHTS", "electricity", "Main Town Meter",
            "POD-MAIN", "kWh", "smartmeter", 1, "2026-01-01", None, None,
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
            "MP-WATER-1", None, "OBJ-RESERVOIR", "water", "Reservoir Water Meter",
            "W-1", "m3", "smartmeter", 1, "2026-01-01", None, None,
        ]
    )

    ws_est = wb["estimation_rules"]
    ws_est.append([None, "MP-TOWN-MAIN", "OBJ-EV", 0.20, "2026-01-01", None, 1, "20% of rest"])
    wb.save(editor_xlsx)
    wb.close()

    assert cli_main(["--data-dir", str(data_dir), "import-editor", str(editor_xlsx)]) == 0

    # Write 1 full 15-min day (2026-02-10 local Europe/Rome) for all meters
    day = datetime(2026, 2, 10, 0, 0)
    rows = []
    rows.extend(
        _make_full_day_rows(day, "MP-TOWN-MAIN", "electricity", "kWh", 1000.0, peak_power_kw=55.0)
    )
    rows.extend(
        _make_full_day_rows(day, "MP-BLDG", "electricity", "kWh", 500.0, peak_power_kw=28.0)
    )
    rows.extend(
        _make_full_day_rows(day, "MP-LIB", "electricity", "kWh", 120.0, peak_power_kw=9.0)
    )
    rows.extend(
        _make_full_day_rows(day, "MP-SCHOOL", "electricity", "kWh", 300.0, peak_power_kw=18.0)
    )
    rows.extend(_make_full_day_rows(day, "MP-WATER-1", "water", "m3", 48.0))

    store = ParquetMeasurementStore(data_dir / "parquet")
    store.write(pl.DataFrame(rows))

    # Run dbt run & dbt test via `python -m dbt.cli.main`
    duckdb_path = data_dir / "test_analytics.duckdb"
    parquet_glob = (
        data_dir / "parquet" / "energy_carrier=*" / "year=*" / "*.parquet"
    ).as_posix()
    sqlite_path = (data_dir / "db" / "masterdata.sqlite").as_posix()

    env = os.environ.copy()
    env["EM_TOOLS_DUCKDB_PATH"] = duckdb_path.as_posix()
    env["EM_TOOLS_SQLITE_PATH"] = sqlite_path
    env["EM_TOOLS_PARQUET_GLOB"] = parquet_glob
    env["EM_TOOLS_DUCKDB_EXT_DIR"] = (REPO_ROOT / ".duckdb_extensions").as_posix()

    dbt_base = [
        sys.executable,
        "-m",
        "dbt.cli.main",
        "--profiles-dir",
        str(DBT_DIR),
        "--project-dir",
        str(DBT_DIR),
    ]

    run_proc = subprocess.run(
        [*dbt_base[:3], "run", *dbt_base[3:]],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert run_proc.returncode == 0, f"dbt run failed:\n{run_proc.stdout}\n{run_proc.stderr}"

    test_proc = subprocess.run(
        [*dbt_base[:3], "test", *dbt_base[3:]],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert test_proc.returncode == 0, f"dbt test failed:\n{test_proc.stdout}\n{test_proc.stderr}"

    # Verify exact multi-level mixed-class arithmetic and derived meter levels in marts
    with duckdb.connect(str(duckdb_path), read_only=True) as con:
        q_rows = con.execute(
            """
            select
                object_id,
                energy_carrier,
                object_class,
                allocation_type,
                round(total_consumption, 4) as total_consumption
            from mart_quarterly_consumption
            where year_quarter = '2026-Q1'
            order by energy_carrier, object_id
            """
        ).fetchall()

        tree_rows = con.execute(
            """
            select metering_point_id, meter_level, is_main_meter, tree_path
            from mart_plausibility_summary
            where year_quarter = '2026-Q1'
            order by tree_path
            """
        ).fetchall()

    results = {(r[0], r[1]): (r[2], r[3], r[4]) for r in q_rows}
    assert results[("OBJ-STREETLIGHTS", "electricity")] == (
        "public_lighting", "residual", pytest.approx(160.0)
    )
    assert results[("OBJ-EV", "electricity")] == (
        "mobility", "estimated_share", pytest.approx(40.0)
    )
    assert results[("OBJ-TOWNHALL", "electricity")] == (
        "building", "residual", pytest.approx(380.0)
    )
    assert results[("OBJ-LIBRARY", "electricity")] == (
        "building", "measured", pytest.approx(120.0)
    )
    assert results[("OBJ-SCHOOL", "electricity")] == (
        "school", "measured", pytest.approx(300.0)
    )
    assert results[("OBJ-RESERVOIR", "water")] == (
        "water_supply", "measured", pytest.approx(48.0)
    )

    # Energy conservation: all electricity allocations sum to main meter gross (1000.0)
    electricity_total = sum(
        val for (_, carrier), (_, _, val) in results.items() if carrier == "electricity"
    )
    assert electricity_total == pytest.approx(1000.0)

    # Verify automatic recursive tree depth & paths
    tree_map = {r[0]: (r[1], r[2], r[3]) for r in tree_rows}
    assert tree_map["MP-TOWN-MAIN"] == (0, True, "MP-TOWN-MAIN")
    assert tree_map["MP-BLDG"] == (1, False, "MP-TOWN-MAIN > MP-BLDG")
    assert tree_map["MP-LIB"] == (2, False, "MP-TOWN-MAIN > MP-BLDG > MP-LIB")
    assert tree_map["MP-SCHOOL"] == (1, False, "MP-TOWN-MAIN > MP-SCHOOL")
    assert tree_map["MP-WATER-1"] == (0, True, "MP-WATER-1")
