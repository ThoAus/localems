"""High-level auto-detecting ingestion pipeline for single files and raw directories."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from localems.config import LocalEmsConfig
from localems.readers.registry import DEFAULT_REGISTRY, ReaderRegistry
from localems.storage import ParquetMeasurementStore


@dataclass(frozen=True)
class IngestFileResult:
    """Summary of ingesting a single raw input file into Parquet."""

    file_path: Path
    reader_name: str
    metering_point_ids: tuple[str, ...]
    channels: tuple[str, ...]
    rows_input: int
    partitions_written: int
    rows_stored: int
    partition_files: tuple[Path, ...]


@dataclass
class IngestBatchResult:
    """Summary of auto-ingesting a folder of heterogeneous raw files."""

    ingested: list[IngestFileResult] = field(default_factory=list)
    skipped: list[tuple[Path, str]] = field(default_factory=list)

    def format_summary(self) -> str:
        lines = [
            f"Batch ingest complete: {len(self.ingested)} file(s) ingested, "
            f"{len(self.skipped)} skipped."
        ]
        for res in self.ingested:
            mps = ", ".join(res.metering_point_ids)
            chs = ", ".join(res.channels)
            lines.append(
                f"  [OK] {res.file_path.name} -> reader={res.reader_name}, "
                f"mp=[{mps}], channel=[{chs}], +{res.rows_input} rows"
            )
        for path, reason in self.skipped:
            lines.append(f"  [SKIP] {path.name}: {reason}")
        return "\n".join(lines)


def _is_ignorable_file(path: Path) -> bool:
    """Ignore temporary spreadsheet lock files and hidden dotfiles."""
    name = path.name
    return (
        not path.is_file()
        or name.startswith("~$")
        or name.startswith(".~lock.")
        or name.startswith(".")
    )


def ingest_file(
    path: Path | str,
    *,
    config: LocalEmsConfig,
    reader_name: str | None = None,
    registry: ReaderRegistry = DEFAULT_REGISTRY,
    **reader_kwargs: Any,
) -> IngestFileResult:
    """Auto-detect (or select) a reader, resolve external meter IDs, and upsert into Parquet."""
    file_path = Path(path)
    config.ensure_directories()

    reader = registry.get(reader_name) if reader_name else registry.detect(file_path)

    kwargs: dict[str, Any] = {
        "default_timezone": config.default_timezone,
    }
    if config.db_path.exists():
        kwargs["db_path"] = config.db_path
    kwargs.update(reader_kwargs)

    df = reader.read(file_path, **kwargs)
    store = ParquetMeasurementStore(
        config.parquet_dir,
        compression=config.parquet_compression,
        default_timezone=kwargs["default_timezone"],
    )
    summary = store.write(df)

    mps = tuple(sorted(df["metering_point_id"].unique().to_list())) if not df.is_empty() else ()
    channels = tuple(sorted(df["channel"].unique().to_list())) if not df.is_empty() else ()

    return IngestFileResult(
        file_path=file_path,
        reader_name=reader.name,
        metering_point_ids=mps,
        channels=channels,
        rows_input=summary.rows_input,
        partitions_written=summary.partitions_written,
        rows_stored=summary.rows_stored,
        partition_files=summary.partition_files,
    )


def ingest_directory(
    directory: Path | str,
    *,
    config: LocalEmsConfig,
    recursive: bool = False,
    registry: ReaderRegistry = DEFAULT_REGISTRY,
    **reader_kwargs: Any,
) -> IngestBatchResult:
    """Scan `directory`, auto-detect the reader for each file, and ingest all matched files."""
    dir_path = Path(directory)
    if not dir_path.is_dir():
        raise NotADirectoryError(f"Not a directory: {dir_path}")

    candidates = sorted(dir_path.rglob("*") if recursive else dir_path.iterdir())
    result = IngestBatchResult()

    for file_path in candidates:
        if _is_ignorable_file(file_path):
            continue
        try:
            reader = registry.detect(file_path)
        except (LookupError, ValueError) as exc:
            result.skipped.append((file_path, str(exc)))
            continue

        file_res = ingest_file(
            file_path,
            config=config,
            reader_name=reader.name,
            registry=registry,
            **reader_kwargs,
        )
        result.ingested.append(file_res)

    return result
