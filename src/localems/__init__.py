"""LocalEMS: Local-first Energy Management System toolkit (Parquet, SQLite/.xlsx, dbt-duckdb)."""

from localems.config import LocalEmsConfig
from localems.db import MasterDataRepository, init_database
from localems.ingest import IngestBatchResult, IngestFileResult, ingest_directory, ingest_file
from localems.readers import DEFAULT_REGISTRY, BaseReader, EdynaXlsxReader, StandardCsvReader
from localems.roundtrip import (
    ImportReport,
    RoundtripValidationError,
    export_to_editor,
    import_from_editor,
)
from localems.schema import (
    MEASUREMENT_ARROW_SCHEMA,
    MEASUREMENT_POLARS_SCHEMA,
    SchemaValidationError,
    normalize_measurements,
)
from localems.storage import ParquetMeasurementStore, WriteSummary

__all__ = [
    "BaseReader",
    "DEFAULT_REGISTRY",
    "EdynaXlsxReader",
    "ImportReport",
    "IngestBatchResult",
    "IngestFileResult",
    "LocalEmsConfig",
    "MEASUREMENT_ARROW_SCHEMA",
    "MEASUREMENT_POLARS_SCHEMA",
    "MasterDataRepository",
    "ParquetMeasurementStore",
    "RoundtripValidationError",
    "SchemaValidationError",
    "StandardCsvReader",
    "WriteSummary",
    "export_to_editor",
    "import_from_editor",
    "ingest_directory",
    "ingest_file",
    "init_database",
    "normalize_measurements",
]
__version__ = "0.1.0"
