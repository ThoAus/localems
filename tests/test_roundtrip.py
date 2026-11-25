"""Tests for SQLite .xlsx roundtrip export/import, validation, backup, and soft-delete."""

from __future__ import annotations

from pathlib import Path

import openpyxl
import pytest

from em_tools.db import MasterDataRepository
from em_tools.roundtrip import (
    RoundtripValidationError,
    export_to_editor,
    import_from_editor,
)


def _populate_workbook(xlsx_path: Path) -> None:
    wb = openpyxl.load_workbook(xlsx_path)

    ws_obj = wb["objekte"]
    ws_obj.append(["OBJ-CAMPUS", "Bildungscampus", "gebaeude", "Bildung", "Schulen", 1, "Haupt"])
    ws_obj.append(["OBJ-WING-A", "Trakt A", "gebaeude", "Bildung", "Schulen", 1, None])
    ws_obj.append(["OBJ-GYM", "Turnhalle", "gebaeude", "Sport", "Hallen", 1, "Geschätzt"])

    ws_mp = wb["messpunkte"]
    ws_mp.append(
        [
            "MP-MAIN", "OBJ-CAMPUS", "strom", "Hauptzähler", "POD-1",
            "kWh", "smartmeter", 1, "2026-01-01", None, None,
        ]
    )
    ws_mp.append(
        [
            "MP-SUB-A", "OBJ-WING-A", "strom", "Unterzähler A", "SUB-A",
            "kWh", "submeter", 1, "2026-01-01", None, None,
        ]
    )

    ws_reg = wb["hierarchie_regeln"]
    # Leave rule_id blank -> importer must auto-assign primary keys
    ws_reg.append(
        [
            None, "MP-MAIN", "MP-SUB-A", None, "submeter_deduction",
            1.0, "2026-01-01", None, 1, "Abzug Trakt A",
        ]
    )
    ws_reg.append(
        [
            None, "MP-MAIN", None, "OBJ-GYM", "estimation_factor",
            0.3, "2026-01-01", None, 1, "30% Rest",
        ]
    )

    wb.save(xlsx_path)
    wb.close()


def test_full_roundtrip_auto_rule_id_diff_backup_and_soft_delete(tmp_path: Path) -> None:
    db_path = tmp_path / "db" / "masterdata.sqlite"
    backups_dir = tmp_path / "db" / "backups"
    xlsx_path = tmp_path / "editor" / "masterdata.xlsx"

    repo = MasterDataRepository(db_path, backups_dir=backups_dir)
    repo.init()

    # 1. Export empty DB -> populate in Excel -> dry-run -> commit
    export_to_editor(db_path, xlsx_path)
    _populate_workbook(xlsx_path)

    dry = import_from_editor(db_path, xlsx_path, dry_run=True, backups_dir=backups_dir)
    assert dry.dry_run is True
    assert len(dry.diffs["objekte"].inserts) == 3
    assert len(dry.diffs["messpunkte"].inserts) == 2
    assert len(dry.diffs["hierarchie_regeln"].inserts) == 2
    assert dry.backup_path is None
    assert repo.fetch_table("objekte") == []

    committed = import_from_editor(db_path, xlsx_path, backups_dir=backups_dir)
    assert committed.dry_run is False
    assert committed.assigned_rule_ids == [1, 2]
    assert committed.backup_path is not None and committed.backup_path.exists()

    # 2. Re-export and re-import without changes -> fast hash skip, no backup created
    export_to_editor(db_path, xlsx_path)
    noop = import_from_editor(db_path, xlsx_path, backups_dir=backups_dir)
    assert not noop.has_changes
    assert noop.backup_path is None
    assert noop.diffs["hierarchie_regeln"].unchanged_count == 2

    # 3. Edit one cell + remove one rule row -> column diff update + soft-delete (active=0)
    wb = openpyxl.load_workbook(xlsx_path)
    ws_obj = wb["objekte"]
    for row in range(2, ws_obj.max_row + 1):
        if ws_obj.cell(row=row, column=1).value == "OBJ-CAMPUS":
            ws_obj.cell(row=row, column=5, value="Campus")

    ws_reg = wb["hierarchie_regeln"]
    for row in range(2, ws_reg.max_row + 1):
        if ws_reg.cell(row=row, column=1).value == 2:
            ws_reg.delete_rows(row, 1)
            break
    wb.save(xlsx_path)
    wb.close()

    res = import_from_editor(db_path, xlsx_path, backups_dir=backups_dir)
    assert len(res.diffs["objekte"].updates) == 1
    upd = res.diffs["objekte"].updates[0]
    assert upd.key == "OBJ-CAMPUS"
    assert len(upd.changes) == 1
    assert upd.changes[0].field == "ebene_2"
    assert upd.changes[0].old_value == "Schulen"
    assert upd.changes[0].new_value == "Campus"

    assert res.diffs["hierarchie_regeln"].soft_deletes == [2]
    rules_in_db = {r["rule_id"]: r for r in repo.fetch_table("hierarchie_regeln")}
    assert rules_in_db[1]["active"] == 1
    assert rules_in_db[2]["active"] == 0


def test_roundtrip_rejects_cycles_factor_overflow_and_fk_errors(tmp_path: Path) -> None:
    db_path = tmp_path / "masterdata.sqlite"
    xlsx_path = tmp_path / "bad.xlsx"

    repo = MasterDataRepository(db_path)
    repo.init()
    export_to_editor(db_path, xlsx_path)

    wb = openpyxl.load_workbook(xlsx_path)
    ws_obj = wb["objekte"]
    ws_obj.append(["O1", "Obj 1", "gebaeude", "E1", "E2", 1, None])
    ws_obj.append(["O2", "Obj 2", "gebaeude", "E1", "E2", 1, None])
    ws_obj.append(["O3", "Obj 3", "gebaeude", "E1", "E2", 1, None])

    ws_mp = wb["messpunkte"]
    ws_mp.append(["M1", "O1", "strom", "M1", None, "kWh", "smartmeter", 1, None, None, None])
    ws_mp.append(["M2", "O2", "strom", "M2", None, "kWh", "submeter", 1, None, None, None])
    ws_mp.append(["M3", "O3", "strom", "M3", None, "kWh", "submeter", 1, None, None, None])

    ws_reg = wb["hierarchie_regeln"]
    # Cycle: M1 -> M2 -> M3 -> M1
    ws_reg.append(
        [None, "M1", "M2", None, "submeter_deduction", 1.0, "2026-01-01", None, 1, None]
    )
    ws_reg.append(
        [None, "M2", "M3", None, "submeter_deduction", 1.0, "2026-01-01", None, 1, None]
    )
    ws_reg.append(
        [None, "M3", "M1", None, "submeter_deduction", 1.0, "2026-01-01", None, 1, None]
    )
    # Estimation factor sum > 1.0 on overlapping dates (0.6 + 0.5 = 1.1)
    ws_reg.append(
        [None, "M1", None, "O2", "estimation_factor", 0.6, "2026-01-01", "2026-06-30", 1, None]
    )
    ws_reg.append(
        [None, "M1", None, "O3", "estimation_factor", 0.5, "2026-03-01", None, 1, None]
    )
    # FK error + inverted date range
    ws_reg.append(
        [
            None, "M1", None, "O_MISSING", "estimation_factor",
            0.1, "2026-05-01", "2026-04-01", 1, None,
        ]
    )

    wb.save(xlsx_path)
    wb.close()

    with pytest.raises(RoundtripValidationError) as exc_info:
        import_from_editor(db_path, xlsx_path)

    msg = str(exc_info.value)
    assert "cycle detected" in msg
    assert "sum is 1.1000" in msg
    assert "unknown target_objekt_id 'O_MISSING'" in msg
    assert "valid_to (2026-04-01) is earlier than valid_from (2026-05-01)" in msg
    # Database must remain untouched
    assert repo.fetch_table("objekte") == []
