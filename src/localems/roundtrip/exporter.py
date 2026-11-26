"""Export SQLite master data into a formatted .xlsx workbook (Excel & LibreOffice Calc)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

from localems.db.repository import TABLE_COLUMNS, MasterDataRepository
from localems.roundtrip.hashing import HASH_COLUMN, compute_row_hash, normalize_row

HEADER_FILL = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
HEADER_FONT = Font(color="FFFFFF", bold=True)
META_HEADER_FILL = PatternFill(start_color="D9D9D9", end_color="D9D9D9", fill_type="solid")
META_FONT = Font(color="666666", italic=True)

HELPER_COLUMNS: dict[str, tuple[str, ...]] = {
    "objects": (),
    "metering_points": ("_meter_level", "_tree_path"),
    "estimation_rules": (),
}

DROPDOWN_VALIDATIONS: dict[str, dict[str, str]] = {
    "objects": {
        "active": '"1,0"',
    },
    "metering_points": {
        "energy_carrier": '"electricity,water,gas,heating,other"',
        "meter_type": '"smartmeter,submeter,manual,virtual,pv_production"',
        "active": '"1,0"',
    },
    "estimation_rules": {
        "active": '"1,0"',
    },
}

EDITOR_GUIDE_ROWS = [
    ("LocalEMS — Master-Data Roundtrip Workbook", ""),
    ("Compatible with", "Microsoft Excel and LibreOffice Calc (.xlsx)"),
    (
        "1. Tree hierarchy (metering_points)",
        "Leave 'parent_metering_point_id' EMPTY for main meters (level 0). Set it to the parent "
        "meter ID for sub-meters of arbitrary depth. '_meter_level' and '_tree_path' are "
        "auto-computed.",
    ),
    (
        "2. Automatic rest & mixed groups",
        "Each meter's 'object_id' owns its measured value (for leaf meters) OR its automatic "
        "residual ('the rest' after subtracting direct sub-meters and estimation shares). Parent "
        "and sub-meters must share energy_carrier & unit, while their objects can belong to any "
        "group / sub_group (e.g. Buildings, Schools, Public Lighting).",
    ),
    (
        "3. Photovoltaic (PV) plants",
        "Bidirectional grid meters keep one metering_point_id with Parquet channels 'grid_import' "
        "and 'grid_export'. Add the PV generator with meter_type='pv_production' and set "
        "parent_metering_point_id to the grid meter it feeds into (or leave blank for standalone "
        "full feed-in PV plants).",
    ),
    (
        "4. Adding estimation_rules",
        "Leave 'rule_id' EMPTY on new rows; the importer assigns primary keys automatically. "
        "Active overlapping factors per source meter must sum <= 1.0.",
    ),
    (
        "5. Editing & managed columns",
        "Edit any business column directly. Do NOT edit gray columns ('_meter_level', "
        "'_tree_path', '_row_hash').",
    ),
    (
        "6. Removing rows (soft-delete)",
        "Deleting a row from a sheet marks it inactive (active=0) in SQLite to preserve history.",
    ),
    (
        "7. Dates & backups",
        "Use ISO dates YYYY-MM-DD (leave 'valid_to' blank for open-ended). Import validates FKs, "
        "carrier/unit match, factor sums, and tree cycles, and backs up SQLite before commit.",
    ),
]


def _compute_meter_tree_helpers(rows: list[dict[str, Any]]) -> dict[str, tuple[int, str]]:
    """Compute (meter_level, tree_path) for every metering point row."""
    parent_by_id = {
        str(r["metering_point_id"]): r.get("parent_metering_point_id")
        for r in rows
        if r.get("metering_point_id")
    }
    helpers: dict[str, tuple[int, str]] = {}

    for mpid in parent_by_id:
        path_parts = [mpid]
        seen = {mpid}
        curr = parent_by_id.get(mpid)
        while curr and curr in parent_by_id and curr not in seen:
            path_parts.append(curr)
            seen.add(curr)
            curr = parent_by_id.get(curr)
        path_parts.reverse()
        helpers[mpid] = (len(path_parts) - 1, " > ".join(path_parts))
    return helpers


def export_to_editor(db_path: Path | str, output_xlsx: Path | str) -> Path:
    """Export SQLite master-data tables to a formatted .xlsx workbook with tree helpers & hashes."""
    repo = MasterDataRepository(db_path)
    data = repo.fetch_all()
    mp_helpers = _compute_meter_tree_helpers(data["metering_points"])

    wb = openpyxl.Workbook()
    guide_ws = wb.active
    guide_ws.title = "README_EDITOR"
    for r_idx, (topic, text) in enumerate(EDITOR_GUIDE_ROWS, start=1):
        c1 = guide_ws.cell(row=r_idx, column=1, value=topic)
        c2 = guide_ws.cell(row=r_idx, column=2, value=text)
        if r_idx == 1:
            c1.font = Font(bold=True, size=13)
        else:
            c1.font = Font(bold=True)
        c2.alignment = Alignment(wrap_text=True)
    guide_ws.column_dimensions["A"].width = 36
    guide_ws.column_dimensions["B"].width = 96

    for table_name, columns in TABLE_COLUMNS.items():
        ws = wb.create_sheet(title=table_name)
        extra_helpers = HELPER_COLUMNS[table_name]
        meta_cols = (*extra_helpers, HASH_COLUMN)
        all_cols = [*columns, *meta_cols]

        for c_idx, col_name in enumerate(all_cols, start=1):
            cell = ws.cell(row=1, column=c_idx, value=col_name)
            if col_name in meta_cols:
                cell.fill = META_HEADER_FILL
                cell.font = META_FONT
            else:
                cell.fill = HEADER_FILL
                cell.font = HEADER_FONT

        rows = data[table_name]
        # Order metering_points by tree path for readability in Excel/LibreOffice
        if table_name == "metering_points":
            rows = sorted(
                rows,
                key=lambda r: (
                    r.get("energy_carrier") or "",
                    mp_helpers.get(str(r.get("metering_point_id")), (0, ""))[1],
                ),
            )

        for r_idx, raw_row in enumerate(rows, start=2):
            norm = normalize_row(table_name, raw_row)
            row_hash = compute_row_hash(table_name, norm)
            for c_idx, col_name in enumerate(columns, start=1):
                ws.cell(row=r_idx, column=c_idx, value=norm[col_name])

            offset = len(columns) + 1
            if table_name == "metering_points":
                lvl, tpath = mp_helpers.get(str(norm["metering_point_id"]), (0, ""))
                c_lvl = ws.cell(row=r_idx, column=offset, value=lvl)
                c_path = ws.cell(row=r_idx, column=offset + 1, value=tpath)
                c_lvl.font = META_FONT
                c_path.font = META_FONT
                offset += 2

            hash_cell = ws.cell(row=r_idx, column=offset, value=row_hash)
            hash_cell.font = META_FONT

        last_col_letter = get_column_letter(len(all_cols))
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = f"A1:{last_col_letter}{max(len(rows) + 1, 2)}"

        for c_idx, col_name in enumerate(all_cols, start=1):
            col_letter = get_column_letter(c_idx)
            max_len = len(col_name)
            for r_idx in range(2, len(rows) + 2):
                val = ws.cell(row=r_idx, column=c_idx).value
                if val is not None:
                    max_len = max(max_len, len(str(val)))
            ws.column_dimensions[col_letter].width = min(max(max_len + 3, 12), 46)

        for col_name, formula in DROPDOWN_VALIDATIONS.get(table_name, {}).items():
            c_idx = columns.index(col_name) + 1
            col_letter = get_column_letter(c_idx)
            dv = DataValidation(type="list", formula1=formula, allow_blank=True)
            ws.add_data_validation(dv)
            dv.add(f"{col_letter}2:{col_letter}5000")

    out_path = Path(output_xlsx)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out_path)
    return out_path
