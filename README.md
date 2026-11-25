# em_tools — Energy Data Collection, Meter-Tree Master Data & Quarterly Reporting

`em_tools` is an installable Python library and analytics toolset for collecting 15-minute energy and infrastructure consumption data (~35,040 intervals/year per metering point), storing it in standardized **Apache Parquet**, managing a self-describing meter tree and estimation rules in **SQLite** via an **Excel/LibreOffice `.xlsx` roundtrip**, and producing validated quarterly reports with **dbt + DuckDB**.

> **Living Document:** This file defines the project task, architecture concept, data model, and operational commands. It is kept up to date as the project evolves. Chronological architectural and technical decisions are logged in [steps.md](steps.md).

---

## 1. Task & Requirements

### Use Case & Scope
- **Domain:** Standardized storage and quarterly reporting of 15-minute interval data across physical energy carriers (`electricity`, `water`, `gas`, `heating`, `other`) and flexible reporting groups (`group`, `sub_group` on `objects`).
- **Decoupled Physical vs. Organizational Model:** Physical subtraction compatibility is governed by `energy_carrier` + `unit` on `metering_points` and Parquet, while reporting groupings (`group`, `sub_group`) live strictly on `objects`. Under one electricity main meter, sub-meters can belong to `Buildings` and `Education` while the automatic residual ("the rest") belongs to `Public Lighting`.
- **Language Standard:** All project code, schemas, SQLite tables, Parquet columns, dbt models, CLI flags, tests, and documentation use **consistent English** identifiers. Source-specific parsers map any non-English raw provider headers (such as Italian or German export columns) into the canonical English schema at ingestion time.
- **Cadence:** No real-time requirement; primary focus is **quarterly reporting** (KPIs, recursive meter-tree rollups, automatic residual allocation, plausibility audits, and energy conservation checks).
- **Scale & Storage:** Local directory store (`data/parquet/`), designed for ~10–200 metering points per carrier, partitioned by **`energy_carrier` + `year`**.
- **Timestamp Convention:** **UTC, interval start** (`timestamp[us, tz=UTC]`, e.g., `00:00:00Z` covers `00:00–00:15`). Naive local timestamps in raw files are converted using `Europe/Rome` (configurable) with DST awareness, and projected back to local reporting time in dbt quarterly models.

---

## 2. Target Architecture

```text
+--------------------------+        +------------------------------------+
| Raw Input Files          |        | SQLite (Single Source of Truth)    |
| - Edyna 15-min XLSX      |        | - objects (group, sub_group)       |
| - Tidy CSV / Sub-meters  |        | - metering_points (tree via        |
| - Manual readings        |        |   parent_metering_point_id; NULL   |
+------------+-------------+        |   = main meter, unlimited depth)   |
             |                      | - estimation_rules (valid_from/to) |
             v                      +-----------------+------------------+
   [ Python: em_tools ]                               ^  export-editor
   - Modular Readers (Polars)                         v  import-editor
   - UTC Normalize & Validate              [ .xlsx Roundtrip Workbook ]
   - Idempotent Parquet Write              (Excel / LibreOffice Calc;
             |                              auto _meter_level & _tree_path,
             v                              auto rule_id, hash/field diff,
+--------------------------+                cycle & carrier validation,
| Standardized Parquet     |                soft-delete & DB backup)
| data/parquet/            |
|   energy_carrier=<name>/ |
|     year=<YYYY>/         |
|       measurements.parquet
+------------+-------------+
             |
             +-------------------+--------------------+
                                 |
                                 v
                  +-----------------------------+
                  | DuckDB + dbt (dbt-duckdb)   |
                  | - WITH RECURSIVE meter tree |
                  | - Automatic residuals +     |
                  |   estimation allocations    |
                  | - Quarterly KPI marts       |
                  | - DQ & conservation tests   |
                  +-----------------------------+
```

### A) Standardized Measurement Store (Apache Parquet via Polars + PyArrow)
- **No small-file problem:** Measurements are bundled by **`energy_carrier` and `year`** (`data/parquet/energy_carrier=<carrier>/year=<YYYY>/measurements.parquet`).
- **No rigid folder hierarchies:** Reporting groupings (`group`, `sub_group`) live in SQLite, never in folder trees, so reorganizing assets requires zero Parquet data movement.
- **Idempotent ingestion:** Natural uniqueness key `(metering_point_id, energy_carrier, channel, timestamp, reading_type)` deduplicates re-imported overlapping files (keeping the latest `ingested_at`).

### B) Master Data Tree & Estimation Rules (SQLite + `.xlsx` Roundtrip)
- **Single Source of Truth (`data/db/masterdata.sqlite`):**
  - `objects`: `object_id`, `name`, `group`, `sub_group`, `active`, `notes`
  - `metering_points`: `metering_point_id`, `parent_metering_point_id` (`NULL` = main meter; set = sub-meter of unlimited depth), `object_id` (direct owner for leaf meters, or **automatic residual owner** for parent meters), `energy_carrier`, `name`, `external_id`, `unit`, `meter_type`, `active`, `valid_from`, `valid_to`, `notes`
  - `estimation_rules`: `rule_id` (auto-assigned), `source_metering_point_id`, `target_object_id`, `factor` `(0, 1]`, `valid_from`, `valid_to`, `active`, `notes`
- **Roundtrip Workflow (`export_to_editor` ↔ `import_from_editor`):**
  - Exports a formatted `.xlsx` workbook (Excel & LibreOffice Calc) with dropdown validations, read-only auto-computed `_meter_level` & `_tree_path` helpers on `metering_points`, and a managed `_row_hash` column.
  - Leave `rule_id` empty for new `estimation_rules` rows — primary keys are assigned automatically on import.
  - Pre-commit validation checks FKs, matching `energy_carrier` & `unit` between parent and child meters (while allowing mixed `group` / `sub_group` assignments), DFS cycle detection on `metering_points`, overlapping estimation factor sums `<= 1.0`, and date validity.
  - Supports inserting children and parents in any row order (`PRAGMA defer_foreign_keys = ON` + topological ordering).
  - Rows removed from the workbook are **soft-deleted** (`active = 0`) to preserve historical auditability.
  - Creates an **automatic consistent SQLite backup** in `data/db/backups/` before every commit.

### C) Recursive Tree, PV Energy Balance, Automatic Residuals & Quarterly Reports (`dbt-duckdb`)
- DuckDB attaches `masterdata.sqlite` (read-only) and queries Parquet directly.
- **PV Energy Balance:** Bidirectional grid meters share one `metering_point_id` with channels `grid_import` and `grid_export`; behind-the-meter PV generators use `meter_type = 'pv_production'` linked via `parent_metering_point_id` (or `NULL` for standalone full feed-in plants). For any PV-equipped site:
  - `self_consumption = pv_production - grid_export`
  - `total_site_supply = grid_import + pv_production - grid_export` (used before deducting consumer sub-meters, preventing false negative residuals on sunny days).
- **Layers (`dbt/models/`):**
  - `staging`: `stg_measurements`, `stg_objects`, `stg_metering_points`, `stg_estimation_rules`
  - `intermediate`:
    - `int_metering_point_tree` (`WITH RECURSIVE` deriving `meter_level`, `is_main_meter`, `root_metering_point_id`, `tree_path` with unlimited depth)
    - `int_interval_measurements`, `int_pv_interval_balance` (15-min PV balance), `int_metering_point_daily`
    - `int_hierarchy_residuals` (busbar supply + direct-children subtraction at every node — zero double counting — plus automatic residual calculation and negative-residual detection)
    - `int_object_daily_allocated` (combines `measured`, `residual`, and `estimated_share` rows across mixed `group` / `sub_group` targets)
  - `marts`: `mart_quarterly_consumption`, `mart_quarterly_pv_summary` (production, feed-in, grid import, self-consumption rate, autarky rate), `mart_plausibility_summary`
- **Data Quality Tests (`dbt/tests/`):** duplicate intervals, negative consumptions, negative parent residuals, estimation factor bounds, DST-aware active-day 15-minute completeness (`92 / 96 / 100`), **15-minute PV export vs. production plausibility**, and **per-root energy conservation** (`sum(subtree allocations) == root_grid_import - root_grid_export + subtree_pv_production`).

---

## 3. Canonical Parquet Measurements Schema

| Column | Arrow / Parquet Type | Nullable | Scope | Description |
| :--- | :--- | :--- | :--- | :--- |
| `timestamp` | `timestamp[us, tz=UTC]` | No | Base | UTC interval start (e.g., `00:00Z` covers `00:00–00:15`). |
| `interval_minutes` | `int16` | Yes | Base | Interval width in minutes (`15` default; `null` for irregular manual readings). |
| `metering_point_id` | `string` | No | Base | Stable metering point ID (e.g., POD or internal ID). |
| `object_id` | `string` | Yes | Base | Optional object ID (SQLite `metering_points.object_id` is authoritative in dbt). |
| `energy_carrier` | `string` | No | Base | Physical carrier: `electricity`, `water`, `gas`, `heating`, `other`. |
| `channel` | `string` | No | Base | Flow/register: `consumption`, `grid_import`, `grid_export`, or `pv_production`. |
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
│       ├── storage.py              # Partitioned Parquet store (energy_carrier/year) + dedup
│       ├── cli.py                  # `em-tools` CLI
│       ├── db/
│       │   ├── schema.py           # SQLite DDL (objects, metering_points tree, estimation_rules)
│       │   └── repository.py       # SQLite access & online backups
│       ├── roundtrip/
│       │   ├── hashing.py          # Cell normalization & deterministic row hashes
│       │   ├── validator.py        # FK, carrier/unit match, factor overlap & tree cycle checks
│       │   ├── exporter.py         # export_to_editor (.xlsx + _meter_level/_tree_path helpers)
│       │   └── importer.py         # import_from_editor (diff, soft-delete, backup, commit)
│       └── readers/
│           ├── base.py             # BaseReader ABC
│           ├── registry.py         # Reader registry
│           ├── csv_standard.py     # Generic tidy CSV reader
│           └── edyna.py            # Edyna 15-min XLSX (date + 96 columns) reader
├── dbt/
│   ├── dbt_project.yml
│   ├── profiles.yml                # DuckDB profile attaching SQLite + reading Parquet
│   ├── models/
│   │   ├── staging/                # stg_measurements, stg_objects, stg_metering_points, stg_estimation_rules
│   │   ├── intermediate/           # int_metering_point_tree (recursive), int_hierarchy_residuals, ...
│   │   └── marts/                  # mart_quarterly_consumption, mart_plausibility_summary
│   └── tests/                      # 6 custom DQ & conservation SQL tests
├── notebooks/
│   └── em_tools_workbench.ipynb    # Interactive chapter-by-chapter workbench notebook
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

### 3. Ingest Measurement Files into Parquet (Auto-Detected by Filename)

```powershell
# List available readers
em-tools readers

# Auto-ingest an entire folder (detects reader & channel by filename, resolves external_id via SQLite)
em-tools ingest data/raw

# Auto-ingest a single Edyna file (ATTIVA_<POD>_YYYYMM.xlsx, ATTIVA_S_<POD>_YYYYMM.xlsx, ATTIVA_<POD>P_YYYYMM.xlsx)
em-tools ingest data/raw/ATTIVA_IT005E00143758_202601.xlsx

# Explicit reader for generic CSV files (e.g., water meter)
em-tools ingest data/raw/water_q1.csv --reader csv_standard --separator ";" --decimal-comma --energy-carrier water --unit m3 --metering-point-id MP-WATER-01
```

### 4. Run dbt Transformations, Data Quality Tests & Quarterly Reports

```powershell
# Build staging, recursive tree & residual intermediate models, and quarterly marts
dbt run --project-dir dbt --profiles-dir dbt

# Run schema & custom data quality tests (including energy conservation per root meter)
dbt test --project-dir dbt --profiles-dir dbt
```

### 5. Run Python Test Suite & Linter

```powershell
pytest
ruff check src tests
```

### 6. Interactive Jupyter Workbench

Open [notebooks/em_tools_workbench.ipynb](notebooks/em_tools_workbench.ipynb) in VS Code or JupyterLab to run all workflows interactively (workspace setup, SQLite ↔ `.xlsx` roundtrip, per-format Parquet ingestion, Parquet/DuckDB inspection, `dbt run` / `dbt test`, quarterly mart queries, and an optional mixed-class sandbox demo generator).

```powershell
# Optional: install JupyterLab / ipykernel support
pip install -e ".[analytics,dev,notebook]"
```
