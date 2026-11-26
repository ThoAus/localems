"""Repository helpers for reading, backing up, and writing SQLite master data."""

from __future__ import annotations

import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from localems.db.schema import connect_sqlite, init_database

TABLE_COLUMNS: dict[str, tuple[str, ...]] = {
    "objects": (
        "object_id",
        "name",
        "group",
        "sub_group",
        "active",
        "notes",
    ),
    "metering_points": (
        "metering_point_id",
        "parent_metering_point_id",
        "object_id",
        "energy_carrier",
        "name",
        "external_id",
        "unit",
        "meter_type",
        "active",
        "valid_from",
        "valid_to",
        "notes",
    ),
    "estimation_rules": (
        "rule_id",
        "source_metering_point_id",
        "target_object_id",
        "factor",
        "valid_from",
        "valid_to",
        "active",
        "notes",
    ),
}

PRIMARY_KEYS: dict[str, str] = {
    "objects": "object_id",
    "metering_points": "metering_point_id",
    "estimation_rules": "rule_id",
}


def utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


class MasterDataRepository:
    """High-level interface to the SQLite master-data Single Source of Truth."""

    def __init__(self, db_path: Path | str, backups_dir: Path | str | None = None) -> None:
        self.db_path = Path(db_path)
        self.backups_dir = (
            Path(backups_dir) if backups_dir is not None else self.db_path.parent / "backups"
        )

    def init(self) -> Path:
        return init_database(self.db_path)

    def connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.db_path)

    def create_backup(self, *, label: str = "pre_import") -> Path:
        """Create a consistent timestamped backup of the SQLite database."""
        if not self.db_path.exists():
            raise FileNotFoundError(f"Database does not exist: {self.db_path}")

        self.backups_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        backup_path = self.backups_dir / f"{self.db_path.stem}_{stamp}_{label}.sqlite"

        # Use SQLite's online backup API for transactional consistency
        with connect_sqlite(self.db_path) as src, sqlite3.connect(backup_path) as dst:
            src.backup(dst)
        return backup_path

    def fetch_table(self, table: str) -> list[dict[str, Any]]:
        """Return all rows of a managed master-data table ordered by primary key."""
        if table not in TABLE_COLUMNS:
            raise ValueError(f"Unsupported master-data table: {table}")
        cols = ", ".join(f'"{c}"' for c in TABLE_COLUMNS[table])
        pk = PRIMARY_KEYS[table]
        with self.connect() as conn:
            rows = conn.execute(f'SELECT {cols} FROM "{table}" ORDER BY "{pk}"').fetchall()
        return [dict(row) for row in rows]

    def fetch_all(self) -> dict[str, list[dict[str, Any]]]:
        """Fetch all managed tables as a dict of table_name -> list of row dicts."""
        return {table: self.fetch_table(table) for table in TABLE_COLUMNS}

    def restore_backup(self, backup_path: Path | str) -> None:
        """Restore the SQLite database from a backup file."""
        src = Path(backup_path)
        if not src.exists():
            raise FileNotFoundError(f"Backup file not found: {src}")
        shutil.copy2(src, self.db_path)

    def resolve_metering_point_id(
        self,
        external_id: str,
        *,
        channel: str = "consumption",
        base_external_id: str | None = None,
    ) -> str | None:
        """Resolve an external utility meter key (e.g. POD) to internal `metering_point_id`.

        For `channel == 'pv_production'`:
          1. Match `meter_type = 'pv_production'` with `external_id == external_id`
          2. Match `meter_type = 'pv_production'` with `external_id == base_external_id`
          3. Match `meter_type = 'pv_production'` attached under a parent meter whose
             `external_id == base_external_id`
        For consumer/grid channels (`consumption`, `grid_import`, `grid_export`):
          1. Match `meter_type <> 'pv_production'` with `external_id == external_id`
        """
        if not self.db_path.exists():
            return None

        key = external_id.strip()
        base_key = base_external_id.strip() if base_external_id else None

        with self.connect() as conn:
            if channel == "pv_production":
                row = conn.execute(
                    'SELECT "metering_point_id" FROM "metering_points" '
                    'WHERE "meter_type" = \'pv_production\' AND "external_id" = ? '
                    'ORDER BY "active" DESC LIMIT 1',
                    (key,),
                ).fetchone()
                if row:
                    return str(row["metering_point_id"])

                if base_key:
                    row = conn.execute(
                        'SELECT "metering_point_id" FROM "metering_points" '
                        'WHERE "meter_type" = \'pv_production\' AND "external_id" = ? '
                        'ORDER BY "active" DESC LIMIT 1',
                        (base_key,),
                    ).fetchone()
                    if row:
                        return str(row["metering_point_id"])

                    row = conn.execute(
                        'SELECT c."metering_point_id" '
                        'FROM "metering_points" c '
                        'JOIN "metering_points" p '
                        '  ON c."parent_metering_point_id" = p."metering_point_id" '
                        'WHERE c."meter_type" = \'pv_production\' AND p."external_id" = ? '
                        'ORDER BY c."active" DESC LIMIT 1',
                        (base_key,),
                    ).fetchone()
                    if row:
                        return str(row["metering_point_id"])
                return None

            row = conn.execute(
                'SELECT "metering_point_id" FROM "metering_points" '
                'WHERE "meter_type" <> \'pv_production\' AND "external_id" = ? '
                'ORDER BY "active" DESC LIMIT 1',
                (key,),
            ).fetchone()
            return str(row["metering_point_id"]) if row else None
