# em_tools — Energy Data Collection, Master-Data Roundtrip & Quarterly Reporting

`em_tools` is an installable Python library and analytics toolset for collecting 15-minute energy and infrastructure consumption data (~35,040 intervals/year per metering point), storing it in standardized **Apache Parquet**, managing master data and allocation rules in **SQLite** via an **Excel/LibreOffice `.xlsx` roundtrip**, and producing validated quarterly reports with **dbt + DuckDB**.

> **Living Document:** This file defines the project task, architecture concept, data model, and operational commands. It is kept up to date as the project evolves. Chronological architectural and technical decisions are logged in [steps.md](steps.md).

---

## 1. Task & Requirements

### Use Case & Scope
- **Domain:** Standardized storage and quarterly reporting of 15-minute interval energy and infrastructure data across multiple sectors (`sparte`), including:
  - Electricity (`strom`, e.g., smart meter / grid-operator exports such as Edyna POD files)
  - Drinking water (`trinkwasser`)
  - Public lighting (`beleuchtung`)
  - Buildings & sub-meters (`gebaeude`, manual counter readings, additional meters)
- **Cadence:** No real-time requirement; primary focus is **quarterly reporting** (KPIs, multi-level hierarchy rollups, residual checks, and plausibility audits).
- **Scale & Storage:** Local directory store (`data/parquet/`), designed for ~10–200 metering points per sector, partitioned by **`sparte` + `year`**.
- **Timestamp Convention:** **UTC, interval start** (`timestamp[us, tz=UTC]`, e.g., `00:00:00Z` covers `00:00–00:15`). Naive local timestamps in raw files are converted using `Europe/Rome` (configurable) with DST awareness, and converted back to local reporting time in dbt quarterly models.
- **Priorities:** Open standards, reproducible pipelines, low operational/maintenance overhead, and clean separation between raw data, master data/rules, and transformation logic.

---

## 2. Target Architecture

```text
+--------------------------+        +-----------------------------------+
| Raw Input Files          |        | SQLite (Single Source of Truth)   |
| - Edyna 15-min XLSX      |        | - Objects & groupings (ebene_1/2) |
| - Tidy CSV / Sub-meters  |        | - Metering points (messpunkte)    |
| - Manual readings        |        | - Hierarchy & estimation rules    |
+------------+-------------+        |   (valid_from / valid_to)         |
             |                      +-----------------+-----------------+
             v                                        ^
   [ Python: em_tools ]                               |  export-editor
   - Modular Readers (Polars)                         v  import-editor
   - UTC Normalize & Validate              [ .xlsx Roundtrip Workbook ]
   - Idempotent Parquet Write              (Excel / LibreOffice Calc;
             |                              auto rule_id, hash/field diff,
             v                              validation, soft-delete &
+--------------------------+                automatic SQLite backup)
| Standardized Parquet     |
| data/parquet/            |
|   sparte=<sparte>/       |
|     year=<YYYY>/         |
|       measurements.parquet
+------------+-------------+
             |
             +-------------------+--------------------+
                                 |
                                 v
                  +-----------------------------+
                  | DuckDB + dbt (dbt-duckdb)   |
                  | - staging -> intermediate   |
                  |   -> marts (quarterly KPIs) |
                  | - Multi-level residuals     |
                  | - Data quality tests        |
                  +-----------------------------+
```

### A) Standardized Measurement Store (Apache Parquet via Polars + PyArrow)
- **No small-file problem:** Measurements are bundled by **`sparte` and `year`** (`data/parquet/sparte=<sparte>/year=<YYYY>/measurements.parquet`).
- **No rigid folder hierarchies:** Organizational levels (`ebene_1`, `ebene_2`), object categories (`gebaeude`, `trinkwasser`, `beleuchtung`), and metering points (`messpunkt_id`) are never encoded in folder paths, so reorganizations require zero data movement.
- **Idempotent ingestion:** Natural uniqueness key `(messpunkt_id, sparte, channel, timestamp, reading_type)` deduplicates re-imported overlapping files (keeping the latest `ingested_at`).

### B) Master Data, Hierarchies & Estimation Rules (SQLite + `.xlsx` Roundtrip)
- **Single Source of Truth (`data/db/masterdata.sqlite`):**
  - `objekte`: `objekt_id`, `name`, `objekt_typ`, `ebene_1`, `ebene_2`, `active`, `notes`
  - `messpunkte`: `messpunkt_id`, `objekt_id`, `sparte`, `name`, `external_id`, `unit`, `meter_type`, `active`, `valid_from`, `valid_to`, `notes`
  - `hierarchie_regeln`: `rule_id` (auto-assigned), `parent_messpunkt_id`, `child_messpunkt_id`, `target_objekt_id`, `rule_type` (`submeter_deduction` | `estimation_factor`), `factor`, `valid_from`, `valid_to`, `active`, `notes`
- **Roundtrip Workflow (`export_to_editor` ↔ `import_from_editor`):**
  - Generates a formatted `.xlsx` workbook (usable in Excel and LibreOffice Calc) with dropdown validations and a hidden/managed `_row_hash` column.
  - Leave `rule_id` empty for new rules — primary keys are assigned automatically on import.
  - Fast unchanged detection via `_row_hash` + exact column-by-column diffing.
  - Pre-commit validation checks foreign keys, factor bounds `(0, 1]`, overlapping estimation-factor sums `<= 1.0`, date intervals, same-sector sub-meters, and cycle detection on active sub-meter graphs.
  - Rows removed from the spreadsheet are **soft-deleted** (`active = 0`) to preserve historical auditability and referential integrity.
  - Creates an **automatic consistent SQLite backup** in `data/db/backups/` before every commit.

### C) Transformation, Residual Logic & Quarterly Reports (`dbt-duckdb`)
- DuckDB attaches `masterdata.sqlite` (read-only) and queries Parquet directly.
- **Layers (`dbt/models/`):**
  - `staging`: `stg_measurements`, `stg_objekte`, `stg_messpunkte`, `stg_hierarchie_regeln`
  - `intermediate`: `int_interval_measurements`, `int_messpunkt_daily`, `int_hierarchy_residuals` (multi-level direct sub-meter deduction + residual calculation without double counting), `int_object_daily_allocated`
  - `marts`: `mart_quarterly_consumption`, `mart_plausibility_summary`
- **Data Quality Tests (`dbt/tests/`):** duplicate interval check, negative consumption check, negative parent residual check, estimation factor sum check, and active-day 15-minute completeness check (DST-aware: 92 / 96 / 100 intervals/day).

---

## 3. Canonical Parquet Measurements Schema

| Column | Arrow / Parquet Type | Nullable | Scope | Description |
| :--- | :--- | :--- | :--- | :--- |
| `timestamp` | `timestamp[us, tz=UTC]` | No | Base | UTC interval start (e.g., `00:00Z` covers `00:00–00:15`). |
| `interval_minutes` | `int16` | Yes | Base | Interval width in minutes (`15` default; `null` for irregular manual readings). |
| `messpunkt_id` | `string` | No | Base | Stable metering point ID (e.g., POD or internal ID). |
| `objekt_id` | `string` | Yes | Base | Optional object ID (SQLite `messpunkte.objekt_id` is authoritative in dbt). |
| `sparte` | `string` | No | Base | Sector: `strom`, `trinkwasser`, `beleuchtung`, `waerme`, `gas`, `sonstiges`. |
| `channel` | `string` | No | Base | Register/channel (default `verbrauch`). |
| `value` | `float64` | No | Base | Standardized consumption or measurement quantity. |
| `unit` | `string` | No | Base | Canonical unit (`kWh`, `m3`, `kW`, …). |
| `reading_type` | `string` | No | Base | `interval`, `cumulative`, or `instantaneous`. |
| `quality` | `string` | No | Base | `measured`, `manual`, `estimated`, `interpolated`, or `suspect`. |
| `peak_power_kw` | `float64` | Yes | Electricity | 15-minute peak power (kW). |
| `flow_rate` | `float64` | Yes | Water | Flow rate. |
| `pressure_bar` | `float64` | Yes | Water | Water pressure (bar). |
| `burn_duration_min` | `float64` | Yes | Lighting | Burning / switching duration (minutes). |
| `source` | `string` | No | Base | Reader/source identifier or filename. |
| `ingested_at` | `timestamp[us, tz=UTC]` | No | Base | UTC timestamp of ingestion. |

---

## 4. Project Structure

```text
em_tools/
├── pyproject.toml                  # PEP 621 package metadata, dependencies & CLI entrypoint
├── README.md                       # Task, architecture & usage guide (this file)
├── steps.md                        # Chronological decision log
├── .gitignore
├── src/
│   └── em_tools/
│       ├── __init__.py             # Public library API
│       ├── config.py               # Workspace paths & timezone configuration
│       ├── schema.py               # Canonical Polars/Arrow schema & validation
│       ├── storage.py              # Partitioned Parquet store (sparte/year) + dedup
│       ├── cli.py                  # `em-tools` CLI
│       ├── db/
│       │   ├── schema.py           # SQLite DDL (objekte, messpunkte, hierarchie_regeln)
│       │   └── repository.py       # SQLite access & online backups
│       ├── roundtrip/
│       │   ├── hashing.py          # Cell normalization & deterministic row hashes
│       │   ├── validator.py        # FK, range, overlap & DFS cycle validation
│       │   ├── exporter.py         # export_to_editor (.xlsx)
│       │   └── importer.py         # import_from_editor (diff, soft-delete, backup, commit)
│       └── readers/
│           ├── base.py             # BaseReader ABC
│           ├── registry.py         # Reader registry
│           ├── csv_standard.py     # Generic tidy CSV reader
│           └── edyna.py            # Edyna 15-min XLSX (GIORNO + 96 columns) reader
├── dbt/
│   ├── dbt_project.yml
│   ├── profiles.yml                # DuckDB profile attaching SQLite + reading Parquet
│   ├── models/
│   │   ├── staging/
│   │   ├── intermediate/
│   │   └── marts/
│   └── tests/
└── tests/
    ├── test_storage_and_readers.py
    ├── test_roundtrip.py
    └── test_dbt_pipeline.py
```

---

## 5. Setup & Usage Commands

### Installation (Editable Library + Analytics & Dev Tools)

```powershell
# Create virtual environment (Python 3.11+ / 3.12 recommended)
python -m venv .venv
.venv\Scripts\Activate.ps1

# Install em_tools with dbt-duckdb and pytest/ruff
pip install -e ".[analytics,dev]"
```

### 1. Initialize Workspace & SQLite Database

```powershell
em-tools init
```

### 2. Master-Data Roundtrip (SQLite ↔ `.xlsx`)

```powershell
# Export SQLite master data to data/editor/masterdata_roundtrip.xlsx
em-tools export-editor

# Preview diff & validate without modifying SQLite
em-tools import-editor data/editor/masterdata_roundtrip.xlsx --dry-run

# Validate, create automatic backup, and commit changes to SQLite
em-tools import-editor data/editor/masterdata_roundtrip.xlsx
```

### 3. Ingest Measurement Files into Parquet

```powershell
# List available readers
em-tools readers

# Ingest an Edyna 15-minute electricity .xlsx export
em-tools ingest data/raw/edyna_2026_q1.xlsx --reader edyna_xlsx

# Ingest a generic semicolon/decimal-comma CSV (e.g., drinking water)
em-tools ingest data/raw/water_q1.csv --reader csv_standard --separator ";" --decimal-comma --sparte trinkwasser --unit m3 --messpunkt-id MP-TW-01
```

### 4. Run dbt Transformations, Data Quality Tests & Quarterly Reports

```powershell
# Build staging, intermediate (residuals/allocations), and quarterly marts
dbt run --project-dir dbt --profiles-dir dbt

# Run schema & custom data quality tests (duplicates, negatives, residuals, missing intervals)
dbt test --project-dir dbt --profiles-dir dbt
```

### 5. Run Python Test Suite & Linter

```powershell
pytest
ruff check src tests
```
