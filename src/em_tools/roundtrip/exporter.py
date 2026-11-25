"""Export SQLite master data into a formatted .xlsx workbook (Excel & LibreOffice Calc)."""

from __future__ import annotations

from pathlib import Path

import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

from em_tools.db.repository import TABLE_COLUMNS, MasterDataRepository
from em_tools.roundtrip.hashing import HASH_COLUMN, compute_row_hash, normalize_row

HEADER_FILL = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
HEADER_FONT = Font(color="FFFFFF", bold=True)
HASH_HEADER_FILL = PatternFill(start_color="D9D9D9", end_color="D9D9D9", fill_type="solid")
HASH_FONT = Font(color="666666", italic=True)

DROPDOWN_VALIDATIONS: dict[str, dict[str, str]] = {
    "objekte": {
        "objekt_typ": '"gebaeude,trinkwasser,beleuchtung,sonstiges"',
        "active": '"1,0"',
    },
    "messpunkte": {
        "sparte": '"strom,trinkwasser,beleuchtung,waerme,gas,sonstiges"',
        "meter_type": '"smartmeter,submeter,manual,virtual"',
        "active": '"1,0"',
    },
    "hierarchie_regeln": {
        "rule_type": '"submeter_deduction,estimation_factor"',
        "active": '"1,0"',
    },
}

EDITOR_GUIDE_ROWS = [
    ("em_tools — Master-Data Roundtrip Workbook", ""),
    ("Compatible with", "Microsoft Excel and LibreOffice Calc (.xlsx)"),
    ("1. Editing existing rows", "Modify any business column directly. Do NOT edit '_row_hash'."),
    (
        "2. Adding new hierarchie_regeln",
        "Leave 'rule_id' EMPTY on new rows. The importer automatically assigns a new primary key.",
    ),
    (
        "3. Adding objekte / messpunkte",
        "Provide a unique 'objekt_id' or 'messpunkt_id'. Create objekte before referencing them.",
    ),
    (
        "4. Removing rows (soft-delete)",
        "Deleting a row from a sheet marks it inactive (active=0) in SQLite to preserve history.",
    ),
    (
        "5. Dates",
        "Use ISO format YYYY-MM-DD (e.g. 2026-01-01). Leave 'valid_to' blank for open-ended rules.",
    ),
    (
        "6. Validation & Backup",
        "Import checks FKs, factors, date ranges, overlaps, and cycles, and backs up SQLite first.",
    ),
]


def export_to_editor(db_path: Path | str, output_xlsx: Path | str) -> Path:
    """Export SQLite master-data tables to a formatted .xlsx workbook with row hashes."""
    repo = MasterDataRepository(db_path)
    data = repo.fetch_all()

    wb = openpyxl.Workbook()
    # Replace default sheet with README_EDITOR first, then data sheets
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
    guide_ws.column_dimensions["A"].width = 34
    guide_ws.column_dimensions["B"].width = 88

    for table_name, columns in TABLE_COLUMNS.items():
        ws = wb.create_sheet(title=table_name)
        all_cols = [*columns, HASH_COLUMN]

        # Header row
        for c_idx, col_name in enumerate(all_cols, start=1):
            cell = ws.cell(row=1, column=c_idx, value=col_name)
            if col_name == HASH_COLUMN:
                cell.fill = HASH_HEADER_FILL
                cell.font = HASH_FONT
            else:
                cell.fill = HEADER_FILL
                cell.font = HEADER_FONT

        # Data rows
        rows = data[table_name]
        for r_idx, raw_row in enumerate(rows, start=2):
            norm = normalize_row(table_name, raw_row)
            row_hash = compute_row_hash(table_name, norm)
            for c_idx, col_name in enumerate(columns, start=1):
                ws.cell(row=r_idx, column=c_idx, value=norm[col_name])
            hash_cell = ws.cell(row=r_idx, column=len(all_cols), value=row_hash)
            hash_cell.font = HASH_FONT

        # Auto-filter, freeze pane, column widths
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
            ws.column_dimensions[col_letter].width = min(max(max_len + 3, 12), 42)

        # Add dropdown data validations for enum/flag columns (rows 2..5000)
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
