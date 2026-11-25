"""Import edited .xlsx master data into SQLite with hash check, diff, validation, and backup."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import openpyxl

from em_tools.db.repository import (
    PRIMARY_KEYS,
    TABLE_COLUMNS,
    MasterDataRepository,
    utc_now_iso,
)
from em_tools.roundtrip.hashing import (
    HASH_COLUMN,
    compute_row_hash,
    normalize_cell,
    normalize_row,
)
from em_tools.roundtrip.validator import validate_masterdata_state


@dataclass(frozen=True)
class FieldChange:
    field: str
    old_value: Any
    new_value: Any


@dataclass(frozen=True)
class RowUpdate:
    key: Any
    changes: tuple[FieldChange, ...]


@dataclass
class TableDiff:
    table: str
    inserts: list[dict[str, Any]] = field(default_factory=list)
    updates: list[RowUpdate] = field(default_factory=list)
    soft_deletes: list[Any] = field(default_factory=list)
    unchanged_count: int = 0

    @property
    def has_changes(self) -> bool:
        return bool(self.inserts or self.updates or self.soft_deletes)


@dataclass
class ImportReport:
    dry_run: bool
    diffs: dict[str, TableDiff]
    assigned_rule_ids: list[int] = field(default_factory=list)
    backup_path: Path | None = None

    @property
    def has_changes(self) -> bool:
        return any(d.has_changes for d in self.diffs.values())

    def format_summary(self) -> str:
        mode = "DRY-RUN" if self.dry_run else "COMMITTED"
        lines = [f"Roundtrip import [{mode}]:"]
        for table, diff in self.diffs.items():
            lines.append(
                f"  - {table}: +{len(diff.inserts)} insert(s), "
                f"~{len(diff.updates)} update(s), "
                f"-{len(diff.soft_deletes)} soft-delete(s), "
                f"={diff.unchanged_count} unchanged"
            )
            for upd in diff.updates:
                changed = ", ".join(
                    f"{c.field}: {c.old_value!r} -> {c.new_value!r}" for c in upd.changes
                )
                lines.append(f"      update [{upd.key}]: {changed}")
            for key in diff.soft_deletes:
                lines.append(f"      soft-delete [{key}]: active 1 -> 0")
        if self.assigned_rule_ids:
            lines.append(f"  Assigned new rule_id(s): {self.assigned_rule_ids}")
        if self.backup_path:
            lines.append(f"  Pre-commit DB backup: {self.backup_path}")
        return "\n".join(lines)


def _read_sheet_rows(
    wb: openpyxl.Workbook, table: str
) -> list[tuple[dict[str, Any], str | None]]:
    """Read normalized rows and optional stored _row_hash from worksheet `table`."""
    if table not in wb.sheetnames:
        raise ValueError(f"Workbook is missing required worksheet '{table}'.")

    ws = wb[table]
    raw_headers = [cell.value for cell in ws[1]]
    headers = [str(h).strip() if h is not None else "" for h in raw_headers]

    expected_cols = TABLE_COLUMNS[table]
    missing_cols = [c for c in expected_cols if c not in headers]
    if missing_cols:
        raise ValueError(
            f"Worksheet '{table}' is missing required column(s): {', '.join(missing_cols)}"
        )

    col_indices = {col: headers.index(col) for col in expected_cols}
    hash_idx = headers.index(HASH_COLUMN) if HASH_COLUMN in headers else None

    parsed: list[tuple[dict[str, Any], str | None]] = []
    for row_vals in ws.iter_rows(min_row=2, values_only=True):
        business_vals = [row_vals[col_indices[c]] for c in expected_cols]
        if all(
            v is None or (isinstance(v, str) and v.strip() == "") for v in business_vals
        ):
            continue

        raw_dict = {col: row_vals[col_indices[col]] for col in expected_cols}
        norm_row = normalize_row(table, raw_dict)
        stored_hash = (
            normalize_cell(HASH_COLUMN, row_vals[hash_idx])
            if hash_idx is not None and hash_idx < len(row_vals)
            else None
        )
        parsed.append((norm_row, stored_hash))
    return parsed


def _sort_metering_point_inserts(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Order metering_point inserts parent-before-child so self-FK inserts never fail."""
    by_id = {row["metering_point_id"]: row for row in rows}
    ordered: list[dict[str, Any]] = []
    visited: set[str] = set()

    def visit(mpid: str) -> None:
        if mpid in visited:
            return
        row = by_id[mpid]
        parent = row.get("parent_metering_point_id")
        if parent and parent in by_id and parent not in visited:
            visit(parent)
        visited.add(mpid)
        ordered.append(row)

    for mpid in by_id:
        visit(mpid)
    return ordered


def import_from_editor(
    db_path: Path | str,
    input_xlsx: Path | str,
    *,
    dry_run: bool = False,
    backups_dir: Path | str | None = None,
) -> ImportReport:
    """Import master-data changes from an edited .xlsx workbook into SQLite.

    Workflow:
      1. Parse sheets and normalize cells (ignoring read-only export helper columns).
      2. Compare against SQLite using fast row-hash check + column-level diff.
      3. Treat rows with blank `rule_id` in `estimation_rules` as new inserts.
      4. Soft-delete (set `active = 0`) active DB rows missing from the workbook.
      5. Validate the entire proposed target state (FKs, carrier/unit match, cycles, factors).
      6. Unless `dry_run=True`, create an automatic SQLite backup and commit atomically.
    """
    repo = MasterDataRepository(db_path, backups_dir=backups_dir)
    current_data = repo.fetch_all()

    wb = openpyxl.load_workbook(Path(input_xlsx), data_only=True)
    try:
        sheet_data = {table: _read_sheet_rows(wb, table) for table in TABLE_COLUMNS}
    finally:
        wb.close()

    diffs: dict[str, TableDiff] = {}
    proposed_state: dict[str, list[dict[str, Any]]] = {}

    for table, columns in TABLE_COLUMNS.items():
        pk_col = PRIMARY_KEYS[table]
        existing_by_pk = {
            normalize_row(table, row)[pk_col]: normalize_row(table, row)
            for row in current_data[table]
        }
        existing_hashes = {
            pk: compute_row_hash(table, row) for pk, row in existing_by_pk.items()
        }

        diff = TableDiff(table=table)
        seen_pks: set[Any] = set()
        target_rows: list[dict[str, Any]] = []

        for incoming_row, stored_hash in sheet_data[table]:
            pk_val = incoming_row[pk_col]

            # Auto-assigned rule_id on new estimation_rules rows
            if table == "estimation_rules" and pk_val is None:
                diff.inserts.append(incoming_row)
                target_rows.append(incoming_row)
                continue

            if pk_val not in existing_by_pk:
                diff.inserts.append(incoming_row)
                target_rows.append(incoming_row)
                seen_pks.add(pk_val)
                continue

            seen_pks.add(pk_val)
            existing_row = existing_by_pk[pk_val]
            incoming_hash = compute_row_hash(table, incoming_row)

            # Fast row-hash check: unchanged if both DB hash and stored sheet hash match
            if (
                incoming_hash == existing_hashes[pk_val]
                and (stored_hash is None or stored_hash == existing_hashes[pk_val])
            ):
                diff.unchanged_count += 1
                target_rows.append(existing_row)
                continue

            # Column-level diff
            field_changes = tuple(
                FieldChange(col, existing_row[col], incoming_row[col])
                for col in columns
                if existing_row[col] != incoming_row[col]
            )
            if field_changes:
                diff.updates.append(RowUpdate(key=pk_val, changes=field_changes))
                target_rows.append(incoming_row)
            else:
                diff.unchanged_count += 1
                target_rows.append(existing_row)

        # Soft-delete active DB rows absent from the edited workbook
        for pk_val, existing_row in existing_by_pk.items():
            if pk_val in seen_pks:
                continue
            if existing_row.get("active", 1) == 1:
                diff.soft_deletes.append(pk_val)
                soft_deleted = dict(existing_row)
                soft_deleted["active"] = 0
                target_rows.append(soft_deleted)
            else:
                target_rows.append(existing_row)

        diffs[table] = diff
        proposed_state[table] = target_rows

    # Validate full target state before touching SQLite
    validate_masterdata_state(proposed_state)

    report = ImportReport(dry_run=dry_run, diffs=diffs)
    if dry_run or not report.has_changes:
        return report

    # Create pre-commit SQLite backup
    report.backup_path = repo.create_backup(label="pre_import")
    now_iso = utc_now_iso()

    # Apply all changes atomically in FK-safe order (with deferred self-FK support)
    with repo.connect() as conn:
        conn.execute("PRAGMA defer_foreign_keys = ON;")
        for table in ("objects", "metering_points", "estimation_rules"):
            diff = diffs[table]
            columns = TABLE_COLUMNS[table]
            pk_col = PRIMARY_KEYS[table]

            inserts = (
                _sort_metering_point_inserts(diff.inserts)
                if table == "metering_points"
                else diff.inserts
            )

            for row in inserts:
                if table == "estimation_rules" and row["rule_id"] is None:
                    insert_cols = [c for c in columns if c != "rule_id"] + ["updated_at"]
                    placeholders = ", ".join("?" for _ in insert_cols)
                    values = [row[c] for c in columns if c != "rule_id"] + [now_iso]
                    cur = conn.execute(
                        f"INSERT INTO {table} ({', '.join(insert_cols)}) VALUES ({placeholders})",
                        values,
                    )
                    assert cur.lastrowid is not None
                    report.assigned_rule_ids.append(int(cur.lastrowid))
                else:
                    insert_cols = list(columns) + ["updated_at"]
                    placeholders = ", ".join("?" for _ in insert_cols)
                    values = [row[c] for c in columns] + [now_iso]
                    conn.execute(
                        f"INSERT INTO {table} ({', '.join(insert_cols)}) VALUES ({placeholders})",
                        values,
                    )

            for upd in diff.updates:
                set_parts = [f"{chg.field} = ?" for chg in upd.changes] + ["updated_at = ?"]
                values = [chg.new_value for chg in upd.changes] + [now_iso, upd.key]
                conn.execute(
                    f"UPDATE {table} SET {', '.join(set_parts)} WHERE {pk_col} = ?",
                    values,
                )

            for pk_val in diff.soft_deletes:
                conn.execute(
                    f"UPDATE {table} SET active = 0, updated_at = ? WHERE {pk_col} = ?",
                    (now_iso, pk_val),
                )

        conn.commit()

    return report
