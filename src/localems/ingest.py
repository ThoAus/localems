"""High-level auto-detecting ingestion pipeline with SHA-256 skip tracking & archiving."""

from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import polars as pl

from localems.config import LocalEmsConfig
from localems.db.repository import MasterDataRepository
from localems.readers.registry import DEFAULT_REGISTRY, ReaderRegistry
from localems.storage import ParquetMeasurementStore


def compute_file_sha256(path: Path | str, *, chunk_size: int = 1024 * 1024) -> str:
    """Compute the lowercase hex SHA-256 digest of a file."""
    hasher = hashlib.sha256()
    with Path(path).open("rb") as fh:
        while chunk := fh.read(chunk_size):
            hasher.update(chunk)
    return hasher.hexdigest()


def _is_within_directory(path: Path, directory: Path) -> bool:
    """Return True if `path` is inside `directory`."""
    try:
        path.resolve().relative_to(directory.resolve())
        return True
    except ValueError:
        return False


def _move_to_archive(
    file_path: Path,
    archive_dir: Path,
    *,
    relative_to: Path | None = None,
) -> Path | None:
    """Move `file_path` into `archive_dir` without overwriting any existing archived file.

    Returns the destination path, or None if `file_path` is already inside `archive_dir`.
    """
    if _is_within_directory(file_path, archive_dir):
        return None

    if relative_to is not None:
        try:
            rel = file_path.resolve().relative_to(relative_to.resolve())
            dest = archive_dir / rel
        except ValueError:
            dest = archive_dir / file_path.name
    else:
        dest = archive_dir / file_path.name

    dest.parent.mkdir(parents=True, exist_ok=True)

    # Preserve the exact original filename on collision by placing revisions in a dated subfolder
    if dest.exists():
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        rel_dest = dest.relative_to(archive_dir)
        candidate = archive_dir / "revisions" / stamp / rel_dest
        counter = 1
        while candidate.exists():
            candidate = archive_dir / "revisions" / f"{stamp}_{counter}" / rel_dest
            counter += 1
        dest = candidate
        dest.parent.mkdir(parents=True, exist_ok=True)

    shutil.move(str(file_path), str(dest))
    return dest


@dataclass(frozen=True)
class IngestFileResult:
    """Summary of processing a single raw input file."""

    file_path: Path
    reader_name: str
    metering_point_ids: tuple[str, ...]
    channels: tuple[str, ...]
    rows_input: int
    partitions_written: int
    rows_stored: int
    partition_files: tuple[Path, ...]
    file_sha256: str = ""
    archived_path: Path | None = None
    status: str = "imported"
    previous_ingested_at: str | None = None
    previous_file_name: str | None = None

    @property
    def short_sha256(self) -> str:
        return self.file_sha256[:12] if self.file_sha256 else "-"

    def format_summary(self) -> str:
        if self.status == "skipped_duplicate":
            prev_info = self.previous_ingested_at or "earlier"
            prev_name = (
                f" (as {self.previous_file_name})"
                if self.previous_file_name and self.previous_file_name != self.file_path.name
                else ""
            )
            return (
                f"[SKIP: SAME SHA-256] {self.file_path.name}\n"
                f"  sha256={self.short_sha256} | already imported at {prev_info}{prev_name}\n"
                "  Use --force to re-import anyway."
            )

        tag = "IMPORTED + MOVED" if self.archived_path is not None else "IMPORTED"
        mps = ", ".join(self.metering_point_ids) or "-"
        chs = ", ".join(self.channels) or "-"
        lines = [
            f"[{tag}] {self.file_path.name}",
            (
                f"  reader={self.reader_name} | mp=[{mps}] | channel=[{chs}] | "
                f"+{self.rows_input} rows | sha256={self.short_sha256}"
            ),
        ]
        if self.archived_path is not None:
            lines.append(f"  moved -> {self.archived_path}")
        if self.partition_files:
            lines.append(
                f"  parquet: {self.partitions_written} partition(s), "
                f"{self.rows_stored} total deduplicated rows stored"
            )
            for p in self.partition_files:
                lines.append(f"    -> {p}")
        return "\n".join(lines)


@dataclass
class IngestBatchResult:
    """Summary of auto-ingesting a folder of heterogeneous raw files."""

    source_dir: Path | None = None
    ingested: list[IngestFileResult] = field(default_factory=list)
    skipped_duplicates: list[IngestFileResult] = field(default_factory=list)
    skipped: list[tuple[Path, str]] = field(default_factory=list)
    rows_input: int = 0
    partitions_written: int = 0
    rows_stored: int = 0
    partition_files: tuple[Path, ...] = ()

    @property
    def moved_count(self) -> int:
        return sum(1 for r in self.ingested if r.archived_path is not None)

    def format_summary(self) -> str:
        header = (
            f"Ingestion report ({self.source_dir}):"
            if self.source_dir is not None
            else "Ingestion report:"
        )
        lines = [
            header,
            (
                f"  Summary : {len(self.ingested)} imported ({self.moved_count} moved to archive), "
                f"{len(self.skipped_duplicates)} skipped (same SHA-256), "
                f"{len(self.skipped)} skipped (no matching reader)"
            ),
        ]

        if self.ingested:
            lines.append(
                f"  Parquet : +{self.rows_input} row(s) written across "
                f"{self.partitions_written} partition(s) "
                f"({self.rows_stored} total deduplicated rows stored)"
            )
            for p in self.partition_files:
                lines.append(f"    -> {p}")

        if self.ingested:
            lines.append("")
            lines.append("  Imported files:")
            for res in self.ingested:
                tag = "IMPORTED + MOVED" if res.archived_path is not None else "IMPORTED"
                mps = ", ".join(res.metering_point_ids) or "-"
                chs = ", ".join(res.channels) or "-"
                lines.append(f"    [{tag}] {res.file_path.name}")
                lines.append(
                    f"      reader={res.reader_name} | mp=[{mps}] | channel=[{chs}] | "
                    f"+{res.rows_input} rows | sha256={res.short_sha256}"
                )
                if res.archived_path is not None:
                    lines.append(f"      moved -> {res.archived_path}")

        if self.skipped_duplicates:
            lines.append("")
            lines.append("  Skipped (already imported - same SHA-256):")
            for res in self.skipped_duplicates:
                prev_info = res.previous_ingested_at or "earlier"
                prev_name = (
                    f" (as {res.previous_file_name})"
                    if res.previous_file_name and res.previous_file_name != res.file_path.name
                    else ""
                )
                lines.append(f"    [SKIP: SAME SHA-256] {res.file_path.name}")
                lines.append(
                    f"      sha256={res.short_sha256} | previously imported at "
                    f"{prev_info}{prev_name}"
                )

        if self.skipped:
            lines.append("")
            lines.append("  Skipped (unmatched file):")
            for path, reason in self.skipped:
                lines.append(f"    [SKIP: NO READER] {path.name}")
                lines.append(f"      {reason}")

        if not self.ingested and not self.skipped_duplicates and not self.skipped:
            lines.append("  No files found to process.")

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
    archive: bool = True,
    archive_dir: Path | str | None = None,
    force: bool = False,
    **reader_kwargs: Any,
) -> IngestFileResult:
    """Auto-detect reader, skip same-SHA-256 files, upsert Parquet, and archive."""
    file_path = Path(path)
    config.ensure_directories()
    repo = MasterDataRepository(config.db_path, backups_dir=config.backups_dir)

    reader = registry.get(reader_name) if reader_name else registry.detect(file_path)
    file_sha256 = compute_file_sha256(file_path)

    if not force:
        prev = repo.find_ingested_file_by_sha256(file_sha256)
        if prev is not None:
            return IngestFileResult(
                file_path=file_path,
                reader_name=str(prev["reader_name"]),
                metering_point_ids=tuple(
                    p for p in str(prev["metering_point_ids"]).split(",") if p
                ),
                channels=tuple(c for c in str(prev["channels"]).split(",") if c),
                rows_input=0,
                partitions_written=0,
                rows_stored=0,
                partition_files=(),
                file_sha256=file_sha256,
                archived_path=None,
                status="skipped_duplicate",
                previous_ingested_at=str(prev["ingested_at"]),
                previous_file_name=str(prev["file_name"]),
            )

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

    target_archive_dir = Path(archive_dir) if archive_dir is not None else config.archive_dir
    moved_to: Path | None = None
    if archive:
        moved_to = _move_to_archive(file_path, target_archive_dir)

    repo.record_ingestion(
        file_sha256=file_sha256,
        file_name=file_path.name,
        source_path=str(file_path.resolve()),
        archived_path=str(moved_to.resolve()) if moved_to is not None else None,
        reader_name=reader.name,
        metering_point_ids=mps,
        channels=channels,
        rows_input=summary.rows_input,
    )

    return IngestFileResult(
        file_path=file_path,
        reader_name=reader.name,
        metering_point_ids=mps,
        channels=channels,
        rows_input=summary.rows_input,
        partitions_written=summary.partitions_written,
        rows_stored=summary.rows_stored,
        partition_files=summary.partition_files,
        file_sha256=file_sha256,
        archived_path=moved_to,
        status="imported",
    )


def ingest_directory(
    directory: Path | str,
    *,
    config: LocalEmsConfig,
    recursive: bool = False,
    registry: ReaderRegistry = DEFAULT_REGISTRY,
    archive: bool = True,
    archive_dir: Path | str | None = None,
    force: bool = False,
    **reader_kwargs: Any,
) -> IngestBatchResult:
    """Scan `directory`, skip same-SHA-256 files, batch-write Parquet, and archive."""
    dir_path = Path(directory)
    if not dir_path.is_dir():
        raise NotADirectoryError(f"Not a directory: {dir_path}")

    config.ensure_directories()
    repo = MasterDataRepository(config.db_path, backups_dir=config.backups_dir)
    target_archive_dir = Path(archive_dir) if archive_dir is not None else config.archive_dir

    candidates = sorted(dir_path.rglob("*") if recursive else dir_path.iterdir())
    scanning_inside_archive = _is_within_directory(dir_path, target_archive_dir)
    result = IngestBatchResult(source_dir=dir_path)

    # Collect parsed files first so Parquet partitions are written once per batch
    parsed_items: list[tuple[Path, str, str, pl.DataFrame, tuple[str, ...], tuple[str, ...]]] = []
    seen_hashes_in_batch: dict[str, tuple[str, str]] = {}

    base_kwargs: dict[str, Any] = {
        "default_timezone": config.default_timezone,
    }
    if config.db_path.exists():
        base_kwargs["db_path"] = config.db_path
    base_kwargs.update(reader_kwargs)

    for file_path in candidates:
        if _is_ignorable_file(file_path):
            continue
        if not scanning_inside_archive and _is_within_directory(file_path, target_archive_dir):
            continue
        try:
            reader = registry.detect(file_path)
        except (LookupError, ValueError) as exc:
            result.skipped.append((file_path, str(exc)))
            continue

        file_sha256 = compute_file_sha256(file_path)

        if not force:
            if file_sha256 in seen_hashes_in_batch:
                prev_name, prev_ts = seen_hashes_in_batch[file_sha256]
                result.skipped_duplicates.append(
                    IngestFileResult(
                        file_path=file_path,
                        reader_name=reader.name,
                        metering_point_ids=(),
                        channels=(),
                        rows_input=0,
                        partitions_written=0,
                        rows_stored=0,
                        partition_files=(),
                        file_sha256=file_sha256,
                        archived_path=None,
                        status="skipped_duplicate",
                        previous_ingested_at=prev_ts,
                        previous_file_name=prev_name,
                    )
                )
                continue

            prev = repo.find_ingested_file_by_sha256(file_sha256)
            if prev is not None:
                result.skipped_duplicates.append(
                    IngestFileResult(
                        file_path=file_path,
                        reader_name=str(prev["reader_name"]),
                        metering_point_ids=tuple(
                            p for p in str(prev["metering_point_ids"]).split(",") if p
                        ),
                        channels=tuple(c for c in str(prev["channels"]).split(",") if c),
                        rows_input=0,
                        partitions_written=0,
                        rows_stored=0,
                        partition_files=(),
                        file_sha256=file_sha256,
                        archived_path=None,
                        status="skipped_duplicate",
                        previous_ingested_at=str(prev["ingested_at"]),
                        previous_file_name=str(prev["file_name"]),
                    )
                )
                continue

        df = reader.read(file_path, **base_kwargs)
        mps = (
            tuple(sorted(df["metering_point_id"].unique().to_list())) if not df.is_empty() else ()
        )
        channels = tuple(sorted(df["channel"].unique().to_list())) if not df.is_empty() else ()
        parsed_items.append((file_path, reader.name, file_sha256, df, mps, channels))
        seen_hashes_in_batch[file_sha256] = (file_path.name, "earlier in this batch")

    if not parsed_items:
        return result

    non_empty_dfs = [item[3] for item in parsed_items if not item[3].is_empty()]
    store = ParquetMeasurementStore(
        config.parquet_dir,
        compression=config.parquet_compression,
        default_timezone=base_kwargs["default_timezone"],
    )
    if non_empty_dfs:
        combined_df = pl.concat(non_empty_dfs, how="vertical")
        summary = store.write(combined_df)
        result.rows_input = summary.rows_input
        result.partitions_written = summary.partitions_written
        result.rows_stored = summary.rows_stored
        result.partition_files = summary.partition_files

    # Archive & record ingestion_log only after Parquet write has succeeded atomically
    for file_path, reader_name, file_sha256, df, mps, channels in parsed_items:
        moved_to: Path | None = None
        if archive:
            moved_to = _move_to_archive(
                file_path,
                target_archive_dir,
                relative_to=dir_path if recursive else None,
            )

        repo.record_ingestion(
            file_sha256=file_sha256,
            file_name=file_path.name,
            source_path=str(file_path.resolve()),
            archived_path=str(moved_to.resolve()) if moved_to is not None else None,
            reader_name=reader_name,
            metering_point_ids=mps,
            channels=channels,
            rows_input=df.height,
        )

        result.ingested.append(
            IngestFileResult(
                file_path=file_path,
                reader_name=reader_name,
                metering_point_ids=mps,
                channels=channels,
                rows_input=df.height,
                partitions_written=result.partitions_written,
                rows_stored=result.rows_stored,
                partition_files=result.partition_files,
                file_sha256=file_sha256,
                archived_path=moved_to,
                status="imported",
            )
        )

    return result
