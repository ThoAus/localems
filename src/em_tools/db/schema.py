"""SQLite DDL schema for energy master data, groupings, and hierarchy/estimation rules."""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS objekte (
    objekt_id   TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    objekt_typ  TEXT NOT NULL CHECK (
        objekt_typ IN ('gebaeude', 'trinkwasser', 'beleuchtung', 'sonstiges')
    ),
    ebene_1     TEXT NOT NULL,
    ebene_2     TEXT NOT NULL,
    active      INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    notes       TEXT,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messpunkte (
    messpunkt_id TEXT PRIMARY KEY,
    objekt_id    TEXT NOT NULL REFERENCES objekte(objekt_id),
    sparte       TEXT NOT NULL CHECK (
        sparte IN ('strom', 'trinkwasser', 'beleuchtung', 'waerme', 'gas', 'sonstiges')
    ),
    name         TEXT NOT NULL,
    external_id  TEXT,
    unit         TEXT NOT NULL,
    meter_type   TEXT NOT NULL DEFAULT 'smartmeter' CHECK (
        meter_type IN ('smartmeter', 'submeter', 'manual', 'virtual')
    ),
    active       INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    valid_from   TEXT,
    valid_to     TEXT,
    notes        TEXT,
    updated_at   TEXT NOT NULL,
    CHECK (valid_to IS NULL OR valid_from IS NULL OR valid_to >= valid_from)
);

CREATE TABLE IF NOT EXISTS hierarchie_regeln (
    rule_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_messpunkt_id TEXT NOT NULL REFERENCES messpunkte(messpunkt_id),
    child_messpunkt_id  TEXT REFERENCES messpunkte(messpunkt_id),
    target_objekt_id    TEXT REFERENCES objekte(objekt_id),
    rule_type           TEXT NOT NULL CHECK (
        rule_type IN ('submeter_deduction', 'estimation_factor')
    ),
    factor              REAL NOT NULL DEFAULT 1.0 CHECK (factor > 0.0 AND factor <= 1.0),
    valid_from          TEXT NOT NULL,
    valid_to            TEXT,
    active              INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    notes               TEXT,
    updated_at          TEXT NOT NULL,
    CHECK (valid_to IS NULL OR valid_to >= valid_from),
    CHECK (
        (
            rule_type = 'submeter_deduction'
            AND child_messpunkt_id IS NOT NULL
            AND child_messpunkt_id <> parent_messpunkt_id
        )
        OR
        (rule_type = 'estimation_factor' AND target_objekt_id IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_messpunkte_objekt ON messpunkte(objekt_id);
CREATE INDEX IF NOT EXISTS idx_messpunkte_sparte ON messpunkte(sparte);
CREATE INDEX IF NOT EXISTS idx_regeln_parent ON hierarchie_regeln(parent_messpunkt_id);
CREATE INDEX IF NOT EXISTS idx_regeln_child ON hierarchie_regeln(child_messpunkt_id);
CREATE INDEX IF NOT EXISTS idx_regeln_active ON hierarchie_regeln(active, valid_from, valid_to);
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
