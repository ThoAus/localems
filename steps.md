# Project Decisions Log (`steps.md`)

This file records all important architectural, technical, and organizational decisions for `em_tools` in chronological order. Each entry includes a short title, date, status, and description.

> **Entry Template**
> ```markdown
> ## <Short Title>
> - **Date:** YYYY-MM-DD
> - **Status:** Proposed | Decided | Superseded | Open
> - **Description:** Concise summary of the decision, reasoning, and consequences.
> ```

---

## Project Scope and Core Concept
- **Date:** 2026-11-24
- **Status:** Decided
- **Description:** Defined `em_tools` as a reusable Python library for collecting energy consumption data from heterogeneous sources (smart meters, energy/sub-meters, manual readings, etc.) across multiple input file formats, normalizing the measurements, and storing them in a standardized Apache Parquet format.

## Python Package Layout (`src/` Layout + `pyproject.toml`)
- **Date:** 2026-11-24
- **Status:** Decided
- **Description:** Adopt the PyPA-recommended `src/em_tools/` layout with a single `pyproject.toml` (PEP 621, `hatchling` build backend) targeting Python 3.11+. The `src/` layout ensures tests always run against the installed package rather than accidentally importing local files, and makes building/installing the library via `pip` or `uv` straightforward.

## Modular Reader Plugin Architecture
- **Date:** 2026-11-24
- **Status:** Decided
- **Description:** Isolate each input format and vendor export format in its own reader class under `em_tools.readers` implementing a common `BaseReader` interface, wired together through a reader registry. This keeps format-specific quirks (delimiters, encodings, OBIS parsing, date formats) decoupled from transformation and storage logic.

## Canonical Long-Format Measurement Schema
- **Date:** 2026-11-24
- **Status:** Superseded (see *Canonical Schema with Base and Sector-Specific Fields*, 2026-11-25)
- **Description:** Initial proposal for a generic long-format measurement table (`timestamp`, `interval_end`, `meter_id`, `channel`, `value`, `unit`, `reading_type`, `quality`, `source`, `ingested_at`). Refined on 2026-11-25 to incorporate 15-minute domain fields (`messpunkt_id`, `objekt_id`, `sparte`, `interval_minutes`) and nullable sector-specific columns.

## Partitioned Parquet Storage with Idempotent Ingestion
- **Date:** 2026-11-24
- **Status:** Superseded (see *Parquet Partitioning by Sector and Time*, 2026-11-25)
- **Description:** Initial proposal to partition Parquet files by `meter_id=<id>/year=<YYYY>/`. Superseded on 2026-11-25 to avoid small-file proliferation and rigid per-meter directory structures.

## Data Processing Engine Selection (`polars` + `pyarrow`)
- **Date:** 2026-11-25
- **Status:** Decided
- **Description:** Selected `polars` paired with `pyarrow` as the Python data processing and Parquet engine for faster execution, strict typing, native Arrow/Parquet support, and lower memory footprint compared to `pandas`.

## Quarterly 15-Minute Reporting Scope and Multi-Sector Domain
- **Date:** 2026-11-25
- **Status:** Decided
- **Description:** Integrated project handoff: primary focus is standardized storage and quarterly reporting of 15-minute interval data (~35,040 intervals/year per metering point) across buildings and infrastructure sectors (`strom`, `trinkwasser`, `beleuchtung`, `gebaeude`), with no real-time requirement. Priorities are open standards, reproducible pipelines, low maintenance effort, and clean separation of raw data, metadata/rules, and transformation logic.

## Parquet Partitioning by Sector and Year (Local Store, No Rigid Folder Hierarchy)
- **Date:** 2026-11-25
- **Status:** Decided
- **Description:** Supersedes per-meter partitioning. Measurements are stored in a local directory (`data/parquet/`) designed for ~10–200 metering points per sector, bundled into Apache Parquet files partitioned by `sparte=<sparte>/year=<YYYY>/measurements.parquet`. Organizational groupings (`ebene_1`, `ebene_2`), object types (`gebaeude`, `trinkwasser`, `beleuchtung`), and metering points (`messpunkt_id`) are never encoded in folder paths so reorganizations do not require moving Parquet files.

## Timestamp Convention (UTC, Interval Start)
- **Date:** 2026-11-25
- **Status:** Decided
- **Description:** Store all Parquet timestamps as UTC interval start (`timestamp[us, tz=UTC]`, e.g., `00:00:00Z` covers `00:00–00:15`). Naive local timestamps from raw input files (e.g., Edyna exports in `Europe/Rome`) are converted cleanly to UTC during ingestion with DST awareness, and projected back into the local reporting timezone (`Europe/Rome`) in dbt models for calendar-accurate quarterly rollups.

## Canonical Schema with Base and Sector-Specific Fields
- **Date:** 2026-11-25
- **Status:** Decided
- **Description:** Supersedes the initial generic schema. Parquet measurements combine standardized base fields (`timestamp`, `interval_minutes`, `messpunkt_id`, `objekt_id`, `sparte`, `channel`, `value`, `unit`, `reading_type`, `quality`, `source`, `ingested_at`) with nullable sector-specific fields (`peak_power_kw` for electricity, `flow_rate`/`pressure_bar` for drinking water, `burn_duration_min` for public lighting). Deduplication uses `(messpunkt_id, sparte, channel, timestamp, reading_type)`.

## SQLite Single Source of Truth for Master Data, Hierarchies, and Rules
- **Date:** 2026-11-25
- **Status:** Decided
- **Description:** Store metering point master data (`messpunkte`), two-level organizational hierarchies (`objekte` with `ebene_1`, `ebene_2`), and multi-level sub-meter/estimation rules (`hierarchie_regeln`) in a local SQLite database (`data/db/masterdata.sqlite`). Hierarchies and allocation rules use a time-bounded edge table (`rule_id`, `parent_messpunkt_id`, `child_messpunkt_id`, `target_objekt_id`, `rule_type`, `factor`, `valid_from`, `valid_to`, `active`), enabling multi-level parent residual calculations without double counting and negative-residual checks.

## Spreadsheet Roundtrip Workflow (`.xlsx`) with Soft-Delete, Validation, and Backups
- **Date:** 2026-11-25
- **Status:** Decided
- **Description:** Implemented `export_to_editor` and `import_from_editor` in Python (`openpyxl`) using `.xlsx` as the everyday editor format (compatible with both Excel and LibreOffice Calc). New `hierarchie_regeln` rows leave `rule_id` empty and receive auto-assigned primary keys upon import. Imports perform deterministic `_row_hash` fast-path checks, column-by-column diffing, full target-state validation (FKs, factor bounds, overlapping estimation sums <= 1.0, date ranges, same-sector sub-meters, DFS cycle detection), **soft-delete (`active = 0`)** for rows removed from the workbook, and an automatic online SQLite backup before committing atomically.

## Analytics, Residual Logic, and Quarterly Reports with `dbt-duckdb`
- **Date:** 2026-11-25
- **Status:** Decided
- **Description:** Implemented a `dbt-duckdb` analytics project under `dbt/` attaching `masterdata.sqlite` (read-only via DuckDB's `sqlite` extension) and reading Parquet measurements directly. Models follow `staging` (`stg_*`) -> `intermediate` (`int_interval_measurements`, `int_messpunkt_daily`, `int_hierarchy_residuals`, `int_object_daily_allocated`) -> `marts` (`mart_quarterly_consumption`, `mart_plausibility_summary`), paired with generic schema tests and 5 custom SQL data quality tests (duplicate intervals, negative consumptions, negative parent residuals, estimation factor bounds, and DST-aware active-day 15-minute interval completeness).
