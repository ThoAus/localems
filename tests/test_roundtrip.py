"""Tests for SQLite .xlsx roundtrip: meter tree, helpers, validation, backup, and soft-delete."""

from __future__ import annotations

from pathlib import Path

import openpyxl
import pytest

from localems.db import MasterDataRepository
from localems.roundtrip import (
    RoundtripValidationError,
    export_to_editor,
    import_from_editor,
)


def _populate_workbook(xlsx_path: Path) -> None:
    wb = openpyxl.load_workbook(xlsx_path)

    ws_obj = wb["objects"]
    ws_obj.append(["OBJ-LIGHTS", "Town Center Streetlights", "Public Lighting", "Center", 1, None])
    ws_obj.append(["OBJ-TOWNHALL", "Town Hall", "Buildings", "Administration", 1, None])
    ws_obj.append(["OBJ-SCHOOL", "Primary School", "Education", "Schools", 1, None])
    ws_obj.append(["OBJ-EV", "Public EV Charger", "Mobility", "Charging", 1, "Estimated"])

    ws_mp = wb["metering_points"]
    # Intentionally add child before parent to verify order-independent topological insert!
    ws_mp.append(
        [
            "MP-SUB-SCHOOL", "MP-MAIN", "OBJ-SCHOOL", "electricity", "School Sub-meter",
            "SUB-S", "kWh", "submeter", 1, "2026-01-01", None, None,
        ]
    )
    ws_mp.append(
        [
            "MP-MAIN", None, "OBJ-LIGHTS", "electricity", "Main Town Meter",
            "POD-1", "kWh", "smartmeter", 1, "2026-01-01", None, "Rest -> Streetlights",
        ]
    )
    ws_mp.append(
        [
            "MP-SUB-HALL", "MP-MAIN", "OBJ-TOWNHALL", "electricity", "Town Hall Sub-meter",
            "SUB-H", "kWh", "submeter", 1, "2026-01-01", None, None,
        ]
    )

    ws_est = wb["estimation_rules"]
    # Leave rule_id blank -> importer must auto-assign primary keys
    ws_est.append([None, "MP-MAIN", "OBJ-EV", 0.2, "2026-01-01", None, 1, "20% of residual"])

    wb.save(xlsx_path)
    wb.close()


def test_full_roundtrip_tree_helpers_auto_rule_id_diff_backup_and_soft_delete(
    tmp_path: Path,
) -> None:
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
    assert len(dry.diffs["objects"].inserts) == 4
    assert len(dry.diffs["metering_points"].inserts) == 3
    assert len(dry.diffs["estimation_rules"].inserts) == 1
    assert dry.backup_path is None
    assert repo.fetch_table("objects") == []

    committed = import_from_editor(db_path, xlsx_path, backups_dir=backups_dir)
    assert committed.dry_run is False
    assert committed.assigned_rule_ids == [1]
    assert committed.backup_path is not None and committed.backup_path.exists()

    # 2. Re-export and verify auto-computed _meter_level and _tree_path helper columns
    export_to_editor(db_path, xlsx_path)
    wb_check = openpyxl.load_workbook(xlsx_path, data_only=True)
    ws_mp = wb_check["metering_points"]
    headers = [c.value for c in ws_mp[1]]
    lvl_idx = headers.index("_meter_level")
    path_idx = headers.index("_tree_path")
    exported_tree = {
        row[0]: (row[lvl_idx], row[path_idx])
        for row in ws_mp.iter_rows(min_row=2, values_only=True)
        if row[0]
    }
    wb_check.close()
    assert exported_tree["MP-MAIN"] == (0, "MP-MAIN")
    assert exported_tree["MP-SUB-HALL"] == (1, "MP-MAIN > MP-SUB-HALL")
    assert exported_tree["MP-SUB-SCHOOL"] == (1, "MP-MAIN > MP-SUB-SCHOOL")

    # Re-importing immediately without edits must be a fast hash no-op
    noop = import_from_editor(db_path, xlsx_path, backups_dir=backups_dir)
    assert not noop.has_changes
    assert noop.backup_path is None
    assert noop.diffs["metering_points"].unchanged_count == 3

    # 3. Edit one cell + remove estimation rule -> column diff update + soft-delete (active=0)
    wb = openpyxl.load_workbook(xlsx_path)
    ws_obj = wb["objects"]
    # Columns: object_id (1), name (2), group (3), sub_group (4), active (5), notes (6)
    for row in range(2, ws_obj.max_row + 1):
        if ws_obj.cell(row=row, column=1).value == "OBJ-SCHOOL":
            ws_obj.cell(row=row, column=4, value="Primary")

    ws_est = wb["estimation_rules"]
    for row in range(2, ws_est.max_row + 1):
        if ws_est.cell(row=row, column=1).value == 1:
            ws_est.delete_rows(row, 1)
            break
    wb.save(xlsx_path)
    wb.close()

    res = import_from_editor(db_path, xlsx_path, backups_dir=backups_dir)
    assert len(res.diffs["objects"].updates) == 1
    upd = res.diffs["objects"].updates[0]
    assert upd.key == "OBJ-SCHOOL"
    assert len(upd.changes) == 1
    assert upd.changes[0].field == "sub_group"
    assert upd.changes[0].old_value == "Schools"
    assert upd.changes[0].new_value == "Primary"

    assert res.diffs["estimation_rules"].soft_deletes == [1]
    rules_in_db = {r["rule_id"]: r for r in repo.fetch_table("estimation_rules")}
    assert rules_in_db[1]["active"] == 0


def test_roundtrip_rejects_cycles_carrier_mismatch_factor_overflow_and_fks(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "masterdata.sqlite"
    xlsx_path = tmp_path / "bad.xlsx"

    repo = MasterDataRepository(db_path)
    repo.init()
    export_to_editor(db_path, xlsx_path)

    wb = openpyxl.load_workbook(xlsx_path)
    ws_obj = wb["objects"]
    ws_obj.append(["O1", "Obj 1", "Buildings", "Core", 1, None])
    ws_obj.append(["O2", "Obj 2", "Education", "Schools", 1, None])
    ws_obj.append(["O3", "Obj 3", "Water Supply", "Reservoirs", 1, None])

    ws_mp = wb["metering_points"]
    # Cycle: M1 -> M2 -> M3 -> M1 (parent_metering_point_id links)
    ws_mp.append(
        ["M1", "M3", "O1", "electricity", "M1", None, "kWh", "smartmeter", 1, None, None, None]
    )
    ws_mp.append(
        ["M2", "M1", "O2", "electricity", "M2", None, "kWh", "submeter", 1, None, None, None]
    )
    ws_mp.append(
        ["M3", "M2", "O3", "electricity", "M3", None, "kWh", "submeter", 1, None, None, None]
    )
    # Energy carrier & unit mismatch child under M1
    ws_mp.append(
        ["M_BAD", "M1", "O3", "water", "Bad Water Sub", None, "m3", "submeter", 1, None, None, None]
    )
    # Invalid PV meter on non-electricity carrier + consumer child under PV meter
    ws_mp.append(
        [
            "MP_PV_BAD", None, "O3", "water", "Water PV",
            None, "m3", "pv_production", 1, None, None, None,
        ]
    )
    ws_mp.append(
        [
            "MP_PV_OK", "M1", "O1", "electricity", "Good PV",
            None, "kWh", "pv_production", 1, None, None, None,
        ]
    )
    ws_mp.append(
        [
            "M_UNDER_PV", "MP_PV_OK", "O2", "electricity", "Bad Child",
            None, "kWh", "submeter", 1, None, None, None,
        ]
    )

    ws_est = wb["estimation_rules"]
    # Estimation factor sum > 1.0 on overlapping dates (0.6 + 0.5 = 1.1)
    ws_est.append([None, "M1", "O2", 0.6, "2026-01-01", "2026-06-30", 1, None])
    ws_est.append([None, "M1", "O3", 0.5, "2026-03-01", None, 1, None])
    # FK error + inverted date range
    ws_est.append([None, "M1", "O_MISSING", 0.1, "2026-05-01", "2026-04-01", 1, None])

    wb.save(xlsx_path)
    wb.close()

    with pytest.raises(RoundtripValidationError) as exc_info:
        import_from_editor(db_path, xlsx_path)

    msg = str(exc_info.value)
    assert "cycle detected in meter tree" in msg
    assert "energy_carrier ('water') must match parent 'M1' ('electricity')" in msg
    assert "unit ('m3') must match parent 'M1' ('kWh')" in msg
    assert "meter_type 'pv_production' requires energy_carrier 'electricity'" in msg
    assert "consumer meter cannot have pv_production meter 'MP_PV_OK' as parent" in msg
    assert "sum is 1.1000" in msg
    assert "unknown target_object_id 'O_MISSING'" in msg
    assert "valid_to (2026-04-01) is earlier than valid_from (2026-05-01)" in msg
    # Database must remain untouched
    assert repo.fetch_table("objects") == []
