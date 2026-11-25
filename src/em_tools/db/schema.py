"""SQLite DDL schema for energy master data: objects, meter tree, and estimation rules."""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS objects (
    object_id    TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    object_class TEXT NOT NULL CHECK (
        object_class IN (
            'building', 'school', 'public_lighting', 'water_supply',
            'sports', 'mobility', 'other'
        )
    ),
    level_1      TEXT NOT NULL,
    level_2      TEXT NOT NULL,
    active       INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    notes        TEXT,
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS metering_points (
    metering_point_id        TEXT PRIMARY KEY,
    parent_metering_point_id TEXT REFERENCES metering_points(metering_point_id),
    object_id                TEXT NOT NULL REFERENCES objects(object_id),
    energy_carrier           TEXT NOT NULL CHECK (
        energy_carrier IN ('electricity', 'water', 'gas', 'heating', 'other')
    ),
    name                     TEXT NOT NULL,
    external_id              TEXT,
    unit                     TEXT NOT NULL,
    meter_type               TEXT NOT NULL DEFAULT 'smartmeter' CHECK (
        meter_type IN ('smartmeter', 'submeter', 'manual', 'virtual')
    ),
    active                   INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    valid_from               TEXT,
    valid_to                 TEXT,
    notes                    TEXT,
    updated_at               TEXT NOT NULL,
    CHECK (parent_metering_point_id IS NULL OR parent_metering_point_id <> metering_point_id),
    CHECK (valid_to IS NULL OR valid_from IS NULL OR valid_to >= valid_from)
);

CREATE TABLE IF NOT EXISTS estimation_rules (
    rule_id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    source_metering_point_id TEXT NOT NULL REFERENCES metering_points(metering_point_id),
    target_object_id         TEXT NOT NULL REFERENCES objects(object_id),
    factor                   REAL NOT NULL CHECK (factor > 0.0 AND factor <= 1.0),
    valid_from               TEXT NOT NULL,
    valid_to                 TEXT,
    active                   INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    notes                    TEXT,
    updated_at               TEXT NOT NULL,
    CHECK (valid_to IS NULL OR valid_to >= valid_from)
);

CREATE INDEX IF NOT EXISTS idx_metering_points_parent
    ON metering_points(parent_metering_point_id);
CREATE INDEX IF NOT EXISTS idx_metering_points_object
    ON metering_points(object_id);
CREATE INDEX IF NOT EXISTS idx_metering_points_carrier
    ON metering_points(energy_carrier);
CREATE INDEX IF NOT EXISTS idx_estimation_source
    ON estimation_rules(source_metering_point_id);
CREATE INDEX IF NOT EXISTS idx_estimation_active
    ON estimation_rules(active, valid_from, valid_to);
"""


def connect_sqlite(db_path: Path | str) -> sqlite3.Connection:
    """Open a SQLite connection with foreign keys and Row factory enabled."""
    conn = sqlite3.connect(Path(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def init_database(db_path: Path | str) -> Path:
    """Initialize the SQLite master-data database if it does not exist yet."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with connect_sqlite(path) as conn:
        conn.executescript(SCHEMA_SQL)
        conn.commit()
    return path
