"""em_tools: Energy consumption ingestion, master-data roundtrip, and reporting toolkit."""

from em_tools.config import EmToolsConfig
from em_tools.db import MasterDataRepository, init_database
from em_tools.ingest import IngestBatchResult, IngestFileResult, ingest_directory, ingest_file
from em_tools.readers import DEFAULT_REGISTRY, BaseReader, EdynaXlsxReader, StandardCsvReader
from em_tools.roundtrip import (
    ImportReport,
    RoundtripValidationError,
    export_to_editor,
    import_from_editor,
)
from em_tools.schema import (
    MEASUREMENT_ARROW_SCHEMA,
    MEASUREMENT_POLARS_SCHEMA,
    SchemaValidationError,
    normalize_measurements,
)
from em_tools.storage import ParquetMeasurementStore, WriteSummary

__all__ = [
    "BaseReader",
    "DEFAULT_REGISTRY",
    "EdynaXlsxReader",
    "EmToolsConfig",
    "ImportReport",
    "IngestBatchResult",
    "IngestFileResult",
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
