"""Tests for canonical schema normalization, partitioned Parquet storage, and input readers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import openpyxl
import polars as pl
import pytest

from em_tools.readers import DEFAULT_REGISTRY, EdynaXlsxReader, StandardCsvReader
from em_tools.schema import SchemaValidationError, normalize_measurements
from em_tools.storage import ParquetMeasurementStore


def test_normalize_converts_local_time_to_utc_and_adds_optional_columns() -> None:
    df = pl.DataFrame(
        {
            "timestamp": [datetime(2026, 1, 15, 1, 0), datetime(2026, 7, 15, 2, 0)],
            "interval_minutes": [15, 15],
            "messpunkt_id": ["MP-1", "MP-1"],
            "sparte": ["Strom", "STROM"],
            "value": [12.5, 8.0],
            "unit": ["kWh", "kWh"],
            "source": ["test", "test"],
        }
    )

    norm = normalize_measurements(df, default_timezone="Europe/Rome")
    assert norm["sparte"].to_list() == ["strom", "strom"]
    # Jan is CET (UTC+1) -> 00:00Z; Jul is CEST (UTC+2) -> 00:00Z
    assert norm["timestamp"].to_list() == [
        datetime(2026, 1, 15, 0, 0, tzinfo=UTC),
        datetime(2026, 7, 15, 0, 0, tzinfo=UTC),
    ]
    assert norm["peak_power_kw"].null_count() == 2
    assert norm["channel"].to_list() == ["verbrauch", "verbrauch"]


def test_normalize_rejects_invalid_sparte() -> None:
    df = pl.DataFrame(
        {
            "timestamp": [datetime(2026, 1, 1, 0, 0, tzinfo=UTC)],
            "messpunkt_id": ["MP-1"],
            "sparte": ["unknown_sector"],
            "value": [1.0],
            "unit": ["kWh"],
            "source": ["test"],
        }
    )
    with pytest.raises(SchemaValidationError, match="Invalid sparte"):
        normalize_measurements(df)


def test_parquet_store_partitions_by_sparte_and_year_and_deduplicates(tmp_path: Path) -> None:
    store = ParquetMeasurementStore(tmp_path / "parquet")

    t0 = datetime(2025, 12, 31, 23, 45, tzinfo=UTC)
    t1 = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    df1 = pl.DataFrame(
        {
            "timestamp": [t0, t1, t1],
            "interval_minutes": [15, 15, 15],
            "messpunkt_id": ["MP-EL-1", "MP-EL-1", "MP-W-1"],
            "sparte": ["strom", "strom", "trinkwasser"],
            "value": [10.0, 20.0, 2.5],
            "unit": ["kWh", "kWh", "m3"],
            "peak_power_kw": [40.0, 80.0, None],
            "pressure_bar": [None, None, 4.2],
            "source": ["batch1", "batch1", "batch1"],
            "ingested_at": [
                datetime(2026, 1, 2, 0, 0, tzinfo=UTC),
                datetime(2026, 1, 2, 0, 0, tzinfo=UTC),
                datetime(2026, 1, 2, 0, 0, tzinfo=UTC),
            ],
        }
    )
    s1 = store.write(df1)
    assert s1.partitions_written == 3
    assert (tmp_path / "parquet" / "sparte=strom" / "year=2025" / "measurements.parquet").exists()
    assert (tmp_path / "parquet" / "sparte=strom" / "year=2026" / "measurements.parquet").exists()
    assert (
        tmp_path / "parquet" / "sparte=trinkwasser" / "year=2026" / "measurements.parquet"
    ).exists()

    # Re-ingest overlapping interval with corrected value and newer ingested_at
    df2 = pl.DataFrame(
        {
            "timestamp": [t1],
            "interval_minutes": [15],
            "messpunkt_id": ["MP-EL-1"],
            "sparte": ["strom"],
            "value": [25.0],
            "unit": ["kWh"],
            "peak_power_kw": [100.0],
            "source": ["batch2_fix"],
            "ingested_at": [datetime(2026, 1, 3, 0, 0, tzinfo=UTC)],
        }
    )
    s2 = store.write(df2)
    assert s2.partitions_written == 1
    assert s2.rows_stored == 1

    out = store.read(sparte="strom", year=2026)
    assert out.height == 1
    assert out["value"][0] == pytest.approx(25.0)
    assert out["peak_power_kw"][0] == pytest.approx(100.0)


def test_standard_csv_reader(tmp_path: Path) -> None:
    csv_file = tmp_path / "water.csv"
    csv_file.write_text(
        "timestamp;value;flow_rate;pressure_bar\n"
        "2026-02-01 01:00:00;1,25;5,0;4,1\n"
        "2026-02-01 01:15:00;1,50;6,0;4,0\n",
        encoding="utf-8",
    )

    reader = StandardCsvReader()
    df = reader.read(
        csv_file,
        separator=";",
        decimal_comma=True,
        messpunkt_id="MP-TW-1",
        sparte="trinkwasser",
        unit="m3",
    )
    assert df.height == 2
    assert df["value"].to_list() == [1.25, 1.5]
    assert df["pressure_bar"].to_list() == [4.1, 4.0]
    assert df["timestamp"][0] == datetime(2026, 2, 1, 0, 0, tzinfo=UTC)


def test_edyna_xlsx_reader(tmp_path: Path) -> None:
    xlsx_file = tmp_path / "edyna_export.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "IT001E99999999"

    # Build header: GIORNO + 96 quarter-hour slots + 4 trailing summary cols
    slots = [f"{h:02d}{m:02d}" for h in range(24) for m in (0, 15, 30, 45)]
    ws.append(["GIORNO", *slots, "TOT", "F1", "F2", "F3"])
    ws.append(["15/01/2026", *["1,5" for _ in range(96)], 144.0, 50, 50, 44])
    ws.append(["20260116", *[2.0 for _ in range(96)], 192.0, 64, 64, 64])
    wb.save(xlsx_file)

    reader = DEFAULT_REGISTRY.get("edyna_xlsx")
    assert isinstance(reader, EdynaXlsxReader)
    df = reader.read(xlsx_file)

    assert df.height == 192
    assert df["messpunkt_id"].unique().to_list() == ["IT001E99999999"]
    assert df["sparte"].unique().to_list() == ["strom"]
    assert df["value"].sum() == pytest.approx(96 * 1.5 + 96 * 2.0)
    # 2026-01-15 00:00 Europe/Rome == 2026-01-14 23:00 UTC
    assert df["timestamp"].min() == datetime(2026, 1, 14, 23, 0, tzinfo=UTC)
    assert df["timestamp"].max() == datetime(2026, 1, 16, 22, 45, tzinfo=UTC)
    assert df["timestamp"].max() - df["timestamp"].min() == timedelta(hours=47, minutes=45)
