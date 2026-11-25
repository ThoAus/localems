"""End-to-end integration test: CLI + Parquet + SQLite roundtrip + dbt-duckdb run & test."""

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
    messpunkt_id: str,
    sparte: str,
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
                "messpunkt_id": messpunkt_id,
                "sparte": sparte,
                "value": per_interval,
                "unit": unit,
                "peak_power_kw": peak_power_kw,
                "source": "integration_test",
            }
        )
    return rows


def test_end_to_end_cli_parquet_roundtrip_and_dbt_marts(tmp_path: Path) -> None:
    if importlib.util.find_spec("dbt.cli.main") is None:
        pytest.skip("dbt-duckdb is not installed in this environment")

    data_dir = tmp_path / "data"
    assert cli_main(["--data-dir", str(data_dir), "init"]) == 0
    assert cli_main(["--data-dir", str(data_dir), "export-editor"]) == 0

    editor_xlsx = data_dir / "editor" / "masterdata_roundtrip.xlsx"
    wb = openpyxl.load_workbook(editor_xlsx)

    # 3-level electricity hierarchy + estimation share + drinking water object
    ws_obj = wb["objekte"]
    ws_obj.append(["OBJ-CAMPUS", "Campus Hauptgebäude", "gebaeude", "Bildung", "Campus", 1, None])
    ws_obj.append(["OBJ-WING-A", "Trakt A", "gebaeude", "Bildung", "Campus", 1, None])
    ws_obj.append(["OBJ-CANTEEN", "Mensa", "gebaeude", "Bildung", "Verpflegung", 1, None])
    ws_obj.append(["OBJ-GYM", "Turnhalle", "gebaeude", "Sport", "Hallen", 1, None])
    ws_obj.append(
        ["OBJ-TW-1", "Hochbehälter Nord", "trinkwasser", "Infrastruktur", "Wasser", 1, None]
    )

    ws_mp = wb["messpunkte"]
    ws_mp.append(
        [
            "MP-CAMPUS", "OBJ-CAMPUS", "strom", "Hauptzähler", "POD-C",
            "kWh", "smartmeter", 1, "2026-01-01", None, None,
        ]
    )
    ws_mp.append(
        [
            "MP-WING-A", "OBJ-WING-A", "strom", "Unterzähler Trakt A", "SUB-A",
            "kWh", "submeter", 1, "2026-01-01", None, None,
        ]
    )
    ws_mp.append(
        [
            "MP-CANTEEN", "OBJ-CANTEEN", "strom", "Unterzähler Mensa", "SUB-M",
            "kWh", "submeter", 1, "2026-01-01", None, None,
        ]
    )
    ws_mp.append(
        [
            "MP-TW-1", "OBJ-TW-1", "trinkwasser", "Zähler Hochbehälter", "TW-1",
            "m3", "smartmeter", 1, "2026-01-01", None, None,
        ]
    )

    # MP-CAMPUS (1000) - MP-WING-A (400) = 600 -> 30% OBJ-GYM (180), residual OBJ-CAMPUS (420)
    # MP-WING-A (400)  - MP-CANTEEN (150) = residual OBJ-WING-A (250)
    # MP-CANTEEN (150) = measured OBJ-CANTEEN (150)
    # Sum across all objects = 420 + 180 + 250 + 150 = 1000 (zero double-counting!)
    ws_reg = wb["hierarchie_regeln"]
    ws_reg.append(
        [
            None, "MP-CAMPUS", "MP-WING-A", None, "submeter_deduction",
            1.0, "2026-01-01", None, 1, None,
        ]
    )
    ws_reg.append(
        [
            None, "MP-WING-A", "MP-CANTEEN", None, "submeter_deduction",
            1.0, "2026-01-01", None, 1, None,
        ]
    )
    ws_reg.append(
        [None, "MP-CAMPUS", None, "OBJ-GYM", "estimation_factor", 0.30, "2026-01-01", None, 1, None]
    )
    wb.save(editor_xlsx)
    wb.close()

    assert cli_main(["--data-dir", str(data_dir), "import-editor", str(editor_xlsx)]) == 0

    # Write 1 full 15-min day (2026-02-10 local Europe/Rome) for all meters
    day = datetime(2026, 2, 10, 0, 0)
    rows = []
    rows.extend(_make_full_day_rows(day, "MP-CAMPUS", "strom", "kWh", 1000.0, peak_power_kw=55.0))
    rows.extend(_make_full_day_rows(day, "MP-WING-A", "strom", "kWh", 400.0, peak_power_kw=22.0))
    rows.extend(_make_full_day_rows(day, "MP-CANTEEN", "strom", "kWh", 150.0, peak_power_kw=12.0))
    rows.extend(_make_full_day_rows(day, "MP-TW-1", "trinkwasser", "m3", 48.0))

    store = ParquetMeasurementStore(data_dir / "parquet")
    store.write(pl.DataFrame(rows))

    # Run dbt run & dbt test via `python -m dbt.cli.main` (portable across Windows/POSIX)
    duckdb_path = data_dir / "test_analytics.duckdb"
    parquet_glob = (data_dir / "parquet" / "sparte=*" / "year=*" / "*.parquet").as_posix()
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

    # Verify exact multi-level residual & estimation arithmetic in mart_quarterly_consumption
    with duckdb.connect(str(duckdb_path), read_only=True) as con:
        q_rows = con.execute(
            """
            select
                objekt_id,
                sparte,
                allocation_type,
                round(total_consumption, 4) as total_consumption
            from mart_quarterly_consumption
            where year_quarter = '2026-Q1'
            order by sparte, objekt_id
            """
        ).fetchall()

    results = {(r[0], r[1]): (r[2], r[3]) for r in q_rows}
    assert results[("OBJ-CAMPUS", "strom")] == ("residual", pytest.approx(420.0))
    assert results[("OBJ-GYM", "strom")] == ("estimated_share", pytest.approx(180.0))
    assert results[("OBJ-WING-A", "strom")] == ("residual", pytest.approx(250.0))
    assert results[("OBJ-CANTEEN", "strom")] == ("measured", pytest.approx(150.0))
    assert results[("OBJ-TW-1", "trinkwasser")] == ("measured", pytest.approx(48.0))

    # Total electricity across all objects must equal gross MP-CAMPUS (1000.0)
    strom_total = sum(val for (_, sp), (_, val) in results.items() if sp == "strom")
    assert strom_total == pytest.approx(1000.0)
