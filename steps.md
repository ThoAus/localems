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
- **Status:** Superseded (see *Canonical Schema with Base and Carrier-Specific Fields*, 2026-11-25)
- **Description:** Initial proposal for a generic long-format measurement table (`timestamp`, `interval_end`, `meter_id`, `channel`, `value`, `unit`, `reading_type`, `quality`, `source`, `ingested_at`).

## Partitioned Parquet Storage with Idempotent Ingestion
- **Date:** 2026-11-24
- **Status:** Superseded (see *Parquet Partitioning by Energy Carrier and Year*, 2026-11-25)
- **Description:** Initial proposal to partition Parquet files by `meter_id=<id>/year=<YYYY>/`. Superseded to avoid small-file proliferation and rigid per-meter directory structures.

## Data Processing Engine Selection (`polars` + `pyarrow`)
- **Date:** 2026-11-25
- **Status:** Decided
- **Description:** Selected `polars` paired with `pyarrow` as the Python data processing and Parquet engine for faster execution, strict typing, native Arrow/Parquet support, and lower memory footprint compared to `pandas`.

## Quarterly 15-Minute Reporting Scope and Multi-Carrier Domain
- **Date:** 2026-11-25
- **Status:** Decided
- **Description:** Integrated project handoff: primary focus is standardized storage and quarterly reporting of 15-minute interval data (~35,040 intervals/year per metering point) across municipal buildings and infrastructure, with no real-time requirement. Priorities are open standards, reproducible pipelines, low maintenance effort, and clean separation of raw data, metadata/rules, and transformation logic.

## Parquet Partitioning by Energy Carrier and Year (Local Store, No Rigid Folder Hierarchy)
- **Date:** 2026-11-25
- **Status:** Decided
- **Description:** Measurements are stored locally under `data/parquet/` (~10–200 metering points per carrier) and partitioned by `energy_carrier=<carrier>/year=<YYYY>/measurements.parquet` (`electricity`, `water`, `gas`, `heating`, `other`). Functional classes (`object_class`) and organizational levels (`level_1`, `level_2`) are never encoded in folder paths so reorganizing or reclassifying assets requires zero Parquet data movement.

## Timestamp Convention (UTC, Interval Start)
- **Date:** 2026-11-25
- **Status:** Decided
- **Description:** Store all Parquet timestamps as UTC interval start (`timestamp[us, tz=UTC]`, e.g., `00:00:00Z` covers `00:00–00:15`). Naive local timestamps from raw input files (e.g., Edyna exports in `Europe/Rome`) are converted cleanly to UTC during ingestion with DST awareness, and projected back into the local reporting timezone (`Europe/Rome`) in dbt models for calendar-accurate quarterly rollups.

## Canonical Schema with Base and Carrier-Specific Fields
- **Date:** 2026-11-25
- **Status:** Decided
- **Description:** Parquet measurements combine standardized base fields (`timestamp`, `interval_minutes`, `metering_point_id`, `object_id`, `energy_carrier`, `channel`, `value`, `unit`, `reading_type`, `quality`, `source`, `ingested_at`) with nullable carrier-specific fields (`peak_power_kw` for electricity, `flow_rate`/`pressure_bar` for water, `burn_duration_min` for lighting). Deduplication uses `(metering_point_id, energy_carrier, channel, timestamp, reading_type)`.

## SQLite Single Source of Truth with General Edge Table (`hierarchy_rules`)
- **Date:** 2026-11-25
- **Status:** Superseded (see *Self-Describing Meter Tree, Decoupled Object Classes, and Estimation Rules*, 2026-11-25)
- **Description:** Initial SQLite model separating sub-meter links into a general `hierarchy_rules` edge table alongside `sector` on meters. Superseded once confirmed that meters never change parent under the same ID, every sub-meter feeds from one parent, and mixed functional classes (e.g., building, school, public lighting) can coexist under a single electricity main meter.

## Strict English-Only Naming Across Code, Schemas, Database, and Docs
- **Date:** 2026-11-25
- **Status:** Decided
- **Description:** Standardized the entire repository to consistent English across all SQLite tables, columns, enum values, Parquet partition keys, dbt models, CLI flags, tests, notebooks, and documentation. Non-English raw provider file headers (e.g., Italian or German utility exports) are mapped into the canonical English schema strictly inside their dedicated reader modules.

## Interactive Chapter-Based Jupyter Workbench Notebook
- **Date:** 2026-11-25
- **Status:** Decided
- **Description:** Added `notebooks/em_tools_workbench.ipynb` grouping all operational workflows into documented chapters (Workspace Setup, SQLite ↔ `.xlsx` Master-Data Roundtrip, Per-Format Parquet Ingestion, Parquet/DuckDB Querying, `dbt-duckdb` Models & Quality Tests, and an optional isolated Demo Generator) alongside an optional `[notebook]` dependency extra in `pyproject.toml`.

## Self-Describing Meter Tree, Decoupled Object Classes, and Estimation Rules
- **Date:** 2026-11-25
- **Status:** Decided
- **Description:** Simplified the master-data model to match real-world constraints (immutable meter IDs, single-parent sub-meters, automatic residual calculation, mixed asset classes under one parent, and time-bounded percentage estimates):
  1. **Decoupled `energy_carrier` vs. `object_class`:** Physical medium (`electricity`, `water`, `gas`, `heating`, `other`) + `unit` live on `metering_points` and Parquet partitions; functional classes (`building`, `school`, `public_lighting`, `water_supply`, `sports`, `mobility`, `other`) live on `objects`. Parent and child meters must match on `energy_carrier` and `unit`, while their objects can belong to any class.
  2. **Meter Tree on `metering_points`:** Added self-referencing `parent_metering_point_id` (`NULL` = main meter, level 0; set = sub-meter of unlimited depth). Each meter's `object_id` automatically receives its measured value (for leaf meters) or its automatic residual ("the rest" after subtracting direct children and estimation shares). `.xlsx` exports automatically compute read-only `_meter_level` and `_tree_path` columns; imports validate tree cycles and carrier/unit parity, support parent/child insertion in any row order, soft-delete removed rows (`active = 0`), and back up SQLite before commit.
  3. **Lightweight `estimation_rules` Table:** Dedicated table (`rule_id` auto-assigned, `source_metering_point_id`, `target_object_id`, `factor`, `valid_from`, `valid_to`, `active`) for percentage splits on a meter's net rest.
  4. **Recursive dbt Tree & Energy Conservation Test:** Added `int_metering_point_tree` (`WITH RECURSIVE` deriving `meter_level`, `is_main_meter`, `root_metering_point_id`, `tree_path`) and `assert_energy_conservation_per_root` verifying that every root meter's gross energy equals the sum of all allocations across its subtree.
