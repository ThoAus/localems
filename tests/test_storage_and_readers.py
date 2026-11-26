"""Tests for canonical schema normalization, partitioned Parquet storage, and input readers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import openpyxl
import polars as pl
import pytest

from localems.readers import DEFAULT_REGISTRY, EdynaXlsxReader, StandardCsvReader
from localems.schema import SchemaValidationError, normalize_measurements
from localems.storage import ParquetMeasurementStore


def test_normalize_converts_local_time_to_utc_and_adds_optional_columns() -> None:
    df = pl.DataFrame(
        {
            "timestamp": [datetime(2026, 1, 15, 1, 0), datetime(2026, 7, 15, 2, 0)],
            "interval_minutes": [15, 15],
            "metering_point_id": ["MP-1", "MP-1"],
            "energy_carrier": ["Electricity", "ELECTRICITY"],
            "value": [12.5, 8.0],
            "unit": ["kWh", "kWh"],
            "source": ["test", "test"],
        }
    )

    norm = normalize_measurements(df, default_timezone="Europe/Rome")
    assert norm["energy_carrier"].to_list() == ["electricity", "electricity"]
    # Jan is CET (UTC+1) -> 00:00Z; Jul is CEST (UTC+2) -> 00:00Z
    assert norm["timestamp"].to_list() == [
        datetime(2026, 1, 15, 0, 0, tzinfo=UTC),
        datetime(2026, 7, 15, 0, 0, tzinfo=UTC),
    ]
    assert norm["peak_power_kw"].null_count() == 2
    assert norm["channel"].to_list() == ["consumption", "consumption"]


def test_normalize_rejects_invalid_energy_carrier() -> None:
    df = pl.DataFrame(
        {
            "timestamp": [datetime(2026, 1, 1, 0, 0, tzinfo=UTC)],
            "metering_point_id": ["MP-1"],
            "energy_carrier": ["unknown_carrier"],
            "value": [1.0],
            "unit": ["kWh"],
            "source": ["test"],
        }
    )
    with pytest.raises(SchemaValidationError, match="Invalid energy_carrier"):
        normalize_measurements(df)


def test_normalize_accepts_pv_channels_and_rejects_invalid_channel() -> None:
    ok_df = pl.DataFrame(
        {
            "timestamp": [
                datetime(2026, 6, 1, 10, 0, tzinfo=UTC),
                datetime(2026, 6, 1, 10, 0, tzinfo=UTC),
                datetime(2026, 6, 1, 10, 0, tzinfo=UTC),
            ],
            "metering_point_id": ["MP-GRID", "MP-GRID", "MP-PV"],
            "energy_carrier": ["electricity", "electricity", "electricity"],
            "channel": ["grid_import", "grid_export", "pv_production"],
            "value": [0.5, 4.0, 7.5],
            "unit": ["kWh", "kWh", "kWh"],
            "source": ["test", "test", "test"],
        }
    )
    norm = normalize_measurements(ok_df)
    assert norm["channel"].to_list() == ["grid_import", "grid_export", "pv_production"]

    bad_df = ok_df.with_columns(pl.lit("invalid_channel").alias("channel"))
    with pytest.raises(SchemaValidationError, match="Invalid channel"):
        normalize_measurements(bad_df)


def test_parquet_store_partitions_by_carrier_and_year_and_deduplicates(tmp_path: Path) -> None:
    store = ParquetMeasurementStore(tmp_path / "parquet")

    t0 = datetime(2025, 12, 31, 23, 45, tzinfo=UTC)
    t1 = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    df1 = pl.DataFrame(
        {
            "timestamp": [t0, t1, t1],
            "interval_minutes": [15, 15, 15],
            "metering_point_id": ["MP-EL-1", "MP-EL-1", "MP-W-1"],
            "energy_carrier": ["electricity", "electricity", "water"],
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
    assert (
        tmp_path
        / "parquet"
        / "energy_carrier=electricity"
        / "year=2025"
        / "measurements.parquet"
    ).exists()
    assert (
        tmp_path
        / "parquet"
        / "energy_carrier=electricity"
        / "year=2026"
        / "measurements.parquet"
    ).exists()
    assert (
        tmp_path
        / "parquet"
        / "energy_carrier=water"
        / "year=2026"
        / "measurements.parquet"
    ).exists()

    # Re-ingest overlapping interval with corrected value and newer ingested_at
    df2 = pl.DataFrame(
        {
            "timestamp": [t1],
            "interval_minutes": [15],
            "metering_point_id": ["MP-EL-1"],
            "energy_carrier": ["electricity"],
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

    out = store.read(energy_carrier="electricity", year=2026)
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
        metering_point_id="MP-DW-1",
        energy_carrier="water",
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

    # Build raw Edyna-format header: provider date column + 96 quarter-hour slots + 4 summary cols
    slots = [f"{h:02d}{m:02d}" for h in range(24) for m in (0, 15, 30, 45)]
    ws.append(["GIORNO", *slots, "TOT", "F1", "F2", "F3"])
    ws.append(["15/01/2026", *["1,5" for _ in range(96)], 144.0, 50, 50, 44])
    ws.append(["20260116", *[2.0 for _ in range(96)], 192.0, 64, 64, 64])
    wb.save(xlsx_file)

    reader = DEFAULT_REGISTRY.get("edyna_xlsx")
    assert isinstance(reader, EdynaXlsxReader)
    df = reader.read(xlsx_file)

    assert df.height == 192
    assert df["metering_point_id"].unique().to_list() == ["IT001E99999999"]
    assert df["energy_carrier"].unique().to_list() == ["electricity"]
    assert df["value"].sum() == pytest.approx(96 * 1.5 + 96 * 2.0)
    # 2026-01-15 00:00 Europe/Rome == 2026-01-14 23:00 UTC
    assert df["timestamp"].min() == datetime(2026, 1, 14, 23, 0, tzinfo=UTC)
    assert df["timestamp"].max() == datetime(2026, 1, 16, 22, 45, tzinfo=UTC)
    assert df["timestamp"].max() - df["timestamp"].min() == timedelta(hours=47, minutes=45)
