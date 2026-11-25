"""Tests for Edyna ATTIVA_*.xlsx filename auto-detection, EU DST transitions, and auto-ingest."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import openpyxl
import polars as pl
import pytest

from em_tools import (
    DEFAULT_REGISTRY,
    EdynaXlsxReader,
    EmToolsConfig,
    MasterDataRepository,
    ParquetMeasurementStore,
    export_to_editor,
    import_from_editor,
    ingest_directory,
)
from em_tools.cli import main as cli_main
from em_tools.readers.edyna import parse_edyna_filename


def _edyna_headers_100() -> list[str]:
    headers = ["GIORNO"]
    quarters = ("I° Q", "II° Q", "III° Q", "IV° Q")
    for h in range(24):
        for q in quarters:
            headers.append(f"Ora {h + 1} {h:02d} - {h + 1:02d} {q}")
    for q in quarters:
        headers.append(f"Ora 25 02 - 03 {q}")
    return headers


def _write_edyna_file(
    path: Path,
    sheet_name: str,
    day_rows: list[tuple[str, list[str | float], list[str | float]]],
) -> Path:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet_name
    ws.append(_edyna_headers_100())
    for day_str, vals_96, vals_ora25 in day_rows:
        ws.append([day_str, *vals_96, *vals_ora25])
    # Trailing empty row like real Edyna exports
    ws.append([""] * 101)
    wb.save(path)
    wb.close()
    return path


def test_parse_edyna_filename_and_registry_auto_detection() -> None:
    m_imp = parse_edyna_filename("ATTIVA_IT005E00143758_202601.xlsx")
    assert m_imp is not None
    assert m_imp.channel == "grid_import"
    assert m_imp.external_key == "IT005E00143758"
    assert m_imp.base_external_key == "IT005E00143758"

    m_exp = parse_edyna_filename("ATTIVA_S_IT005E00143758_202601.xlsx")
    assert m_exp is not None
    assert m_exp.channel == "grid_export"
    assert m_exp.external_key == "IT005E00143758"

    m_pv = parse_edyna_filename("ATTIVA_IT005E00143758P_202601.xlsx")
    assert m_pv is not None
    assert m_pv.channel == "pv_production"
    assert m_pv.external_key == "IT005E00143758P"
    assert m_pv.base_external_key == "IT005E00143758"

    assert parse_edyna_filename("random_other_file.xlsx") is None

    assert isinstance(
        DEFAULT_REGISTRY.detect(Path("ATTIVA_IT005E00143758_202601.xlsx")), EdynaXlsxReader
    )
    assert isinstance(
        DEFAULT_REGISTRY.detect(Path("ATTIVA_S_IT005E00143758_202601.xlsx")), EdynaXlsxReader
    )
    assert isinstance(
        DEFAULT_REGISTRY.detect(Path("ATTIVA_IT005E00143758P_202601.xlsx")), EdynaXlsxReader
    )
    with pytest.raises(LookupError, match="No reader automatically matched"):
        DEFAULT_REGISTRY.detect(Path("unknown_export.txt"))


def test_edyna_dst_handling_normal_24h_spring_23h_and_autumn_25h(tmp_path: Path) -> None:
    """Verify exact EU DST behavior (Europe/Rome):
    - 2026-01-15 (normal winter day, Ora 25 filled with '0,000') -> 96 UTC intervals
    - 2026-03-29 (spring-forward 23h day, 02:00..02:45 skipped) -> 92 UTC intervals
    - 2026-10-25 (fall-back 25h day, Ora 3 = CEST + Ora 25 = CET) -> 100 UTC intervals
    """
    normal_96 = ["1,000"] * 96
    normal_ora25 = ["0,000"] * 4  # Real Edyna PV/export files put '0,000' here on normal days!

    spring_96 = ["2,000"] * 96
    spring_ora25 = [""] * 4

    # On 2026-10-25, distinguish Ora 3 (indices 8..11 = 02:00..02:45 CEST -> 00:00..00:45 UTC)
    # from Ora 25 (02:00..02:45 CET -> 01:00..01:45 UTC)
    autumn_96 = ["3,000"] * 96
    for idx in range(8, 12):
        autumn_96[idx] = "10,000"  # First 02:00..02:45 (CEST = UTC+2 -> 00:00..00:45 UTC)
    autumn_ora25 = ["20,000"] * 4  # Second 02:00..02:45 (CET = UTC+1 -> 01:00..01:45 UTC)

    xlsx = _write_edyna_file(
        tmp_path / "ATTIVA_IT999E00000001_202610.xlsx",
        "IT999E00000001",
        [
            ("20260115", normal_96, normal_ora25),
            ("29/03/2026", spring_96, spring_ora25),
            ("25/10/2026", autumn_96, autumn_ora25),
        ],
    )

    df = EdynaXlsxReader().read(xlsx, default_timezone="Europe/Rome")
    assert df["channel"].unique().to_list() == ["grid_import"]
    assert df.height == 96 + 92 + 100
    # Zero duplicate timestamps across all 3 days
    assert df["timestamp"].n_unique() == 96 + 92 + 100

    # Verify spring-forward skips nonexistent 02:00 local (00:45 UTC -> 01:00 UTC directly)
    spring_ts = set(df["timestamp"].to_list())
    assert datetime(2026, 3, 29, 0, 45, tzinfo=UTC) in spring_ts  # 01:45 CET
    assert datetime(2026, 3, 29, 1, 0, tzinfo=UTC) in spring_ts   # 03:00 CEST

    # Verify fall-back distinguishes first 02:00 (00:00Z = 10.0) vs second 02:00 (01:00Z = 20.0)
    by_ts = dict(zip(df["timestamp"].to_list(), df["value"].to_list(), strict=True))
    assert by_ts[datetime(2026, 10, 25, 0, 0, tzinfo=UTC)] == pytest.approx(10.0)
    assert by_ts[datetime(2026, 10, 25, 0, 45, tzinfo=UTC)] == pytest.approx(10.0)
    assert by_ts[datetime(2026, 10, 25, 1, 0, tzinfo=UTC)] == pytest.approx(20.0)
    assert by_ts[datetime(2026, 10, 25, 1, 45, tzinfo=UTC)] == pytest.approx(20.0)


def test_auto_ingest_directory_resolves_external_ids_from_sqlite(tmp_path: Path) -> None:
    cfg = EmToolsConfig(data_dir=tmp_path / "data", default_timezone="Europe/Rome")
    cfg.ensure_directories()

    # Register internal metering_point_ids mapped to external POD IT999E00000001
    repo = MasterDataRepository(cfg.db_path, backups_dir=cfg.backups_dir)
    repo.init()
    export_to_editor(cfg.db_path, cfg.default_editor_path)
    wb = openpyxl.load_workbook(cfg.default_editor_path)
    wb["objects"].append(["OBJ-SCHOOL", "School", "Education", "Primary", 1, None])
    wb["metering_points"].append(
        [
            "MP-SCHOOL-GRID", None, "OBJ-SCHOOL", "electricity", "School Grid Meter",
            "IT999E00000001", "kWh", "smartmeter", 1, "2026-01-01", None, None,
        ]
    )
    # Leave PV external_id as base POD (or blank) -> resolves automatically via parent/base POD!
    wb["metering_points"].append(
        [
            "MP-SCHOOL-PV", "MP-SCHOOL-GRID", "OBJ-SCHOOL", "electricity", "School PV",
            None, "kWh", "pv_production", 1, "2026-01-01", None, None,
        ]
    )
    wb.save(cfg.default_editor_path)
    wb.close()
    import_from_editor(cfg.db_path, cfg.default_editor_path, backups_dir=cfg.backups_dir)

    # Drop all 3 Edyna files + an ignorable Excel lock file + an unknown .txt file into data/raw
    _write_edyna_file(
        cfg.raw_dir / "ATTIVA_IT999E00000001_202601.xlsx",
        "IT999E00000001",
        [("20260101", ["0,500"] * 96, [""] * 4)],
    )
    _write_edyna_file(
        cfg.raw_dir / "ATTIVA_S_IT999E00000001_202601.xlsx",
        "S_IT999E00000001",
        [("01/01/2026", ["0,100"] * 96, ["0,000"] * 4)],
    )
    _write_edyna_file(
        cfg.raw_dir / "ATTIVA_IT999E00000001P_202601.xlsx",
        "IT999E00000001P",
        [("01/01/2026", ["0,300"] * 96, ["0,000"] * 4)],
    )
    (cfg.raw_dir / "~$ATTIVA_IT999E00000001_202601.xlsx").write_text("lock", encoding="utf-8")
    (cfg.raw_dir / "notes.txt").write_text("unrecognized", encoding="utf-8")

    batch = ingest_directory(cfg.raw_dir, config=cfg)
    assert len(batch.ingested) == 3
    assert len(batch.skipped) == 1
    assert batch.skipped[0][0].name == "notes.txt"

    store = ParquetMeasurementStore(cfg.parquet_dir)
    df = store.read(energy_carrier="electricity")
    assert df.height == 3 * 96

    # Both grid_import & grid_export resolved to MP-SCHOOL-GRID; pv_production -> MP-SCHOOL-PV
    by_ch = {
        row["channel"]: (row["metering_point_id"], row["total"])
        for row in df.group_by(["channel", "metering_point_id"])
        .agg(pl.col("value").sum().alias("total"))
        .to_dicts()
    }
    assert by_ch["grid_import"][0] == "MP-SCHOOL-GRID"
    assert by_ch["grid_import"][1] == pytest.approx(48.0)
    assert by_ch["grid_export"][0] == "MP-SCHOOL-GRID"
    assert by_ch["grid_export"][1] == pytest.approx(9.6)
    assert by_ch["pv_production"][0] == "MP-SCHOOL-PV"
    assert by_ch["pv_production"][1] == pytest.approx(28.8)

    # Verify CLI auto-ingest works with zero --reader flag on the directory
    assert cli_main(["--data-dir", str(cfg.data_dir), "ingest", str(cfg.raw_dir)]) == 0
