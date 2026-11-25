"""Standardized, partitioned Apache Parquet measurement store."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import polars as pl
import pyarrow.parquet as pq

from em_tools.schema import (
    DEDUP_KEY_COLUMNS,
    MEASUREMENT_ARROW_SCHEMA,
    MEASUREMENT_POLARS_SCHEMA,
    normalize_measurements,
)


@dataclass(frozen=True)
class WriteSummary:
    """Summary of an idempotent Parquet write operation."""

    partitions_written: int
    rows_input: int
    rows_stored: int
    partition_files: tuple[Path, ...]


class ParquetMeasurementStore:
    """Reads and writes canonical measurements partitioned by `sparte` and `year`.

    Layout:
        <base_dir>/sparte=<sparte>/year=<YYYY>/measurements.parquet
    """

    def __init__(
        self,
        base_dir: Path | str,
        *,
        compression: str = "zstd",
        default_timezone: str = "Europe/Rome",
    ) -> None:
        self.base_dir = Path(base_dir)
        self.compression = compression
        self.default_timezone = default_timezone

    def partition_path(self, sparte: str, year: int) -> Path:
        return self.base_dir / f"sparte={sparte}" / f"year={year}" / "measurements.parquet"

    def write(self, df: pl.DataFrame) -> WriteSummary:
        """Normalize, deduplicate, and atomically upsert measurements into Parquet partitions."""
        normalized = normalize_measurements(
            df, default_timezone=self.default_timezone
        )
        if normalized.is_empty():
            return WriteSummary(
                partitions_written=0,
                rows_input=0,
                rows_stored=0,
                partition_files=(),
            )

        with_year = normalized.with_columns(
            pl.col("timestamp").dt.year().alias("_partition_year")
        )
        partitions = with_year.partition_by(
            ["sparte", "_partition_year"], as_dict=True, maintain_order=False
        )

        written_files: list[Path] = []
        total_stored = 0

        for (sparte_val, year_val), part_df in partitions.items():
            sparte = str(sparte_val)
            year = int(year_val)
            incoming = part_df.drop("_partition_year")
            target = self.partition_path(sparte, year)
            target.parent.mkdir(parents=True, exist_ok=True)

            if target.exists():
                existing = pl.read_parquet(target)
                combined = pl.concat([existing, incoming], how="vertical")
            else:
                combined = incoming

            # Deduplicate keeping the latest ingested_at per natural key
            deduped = (
                combined.sort("ingested_at")
                .unique(subset=list(DEDUP_KEY_COLUMNS), keep="last")
                .sort(["messpunkt_id", "channel", "timestamp"])
            )

            arrow_table = deduped.to_arrow().cast(MEASUREMENT_ARROW_SCHEMA)
            tmp_path = target.with_suffix(".parquet.tmp")
            pq.write_table(
                arrow_table,
                tmp_path,
                compression=self.compression,
                use_dictionary=True,
            )
            tmp_path.replace(target)

            written_files.append(target)
            total_stored += deduped.height

        return WriteSummary(
            partitions_written=len(written_files),
            rows_input=normalized.height,
            rows_stored=total_stored,
            partition_files=tuple(sorted(written_files)),
        )

    def read(
        self,
        *,
        sparte: str | None = None,
        year: int | None = None,
        messpunkt_ids: list[str] | None = None,
        start_utc: datetime | None = None,
        end_utc: datetime | None = None,
    ) -> pl.DataFrame:
        """Query measurements across partitions with optional pushdown filters."""
        if not self.base_dir.exists():
            return pl.DataFrame(schema=MEASUREMENT_POLARS_SCHEMA)

        pattern = self._glob_pattern(sparte=sparte, year=year)
        files = sorted(self.base_dir.glob(pattern))
        if not files:
            return pl.DataFrame(schema=MEASUREMENT_POLARS_SCHEMA)

        lazy = pl.scan_parquet(files, hive_partitioning=False)
        if sparte is not None:
            lazy = lazy.filter(pl.col("sparte") == sparte.lower())
        if messpunkt_ids is not None:
            lazy = lazy.filter(pl.col("messpunkt_id").is_in(messpunkt_ids))
        if start_utc is not None:
            lazy = lazy.filter(pl.col("timestamp") >= start_utc)
        if end_utc is not None:
            lazy = lazy.filter(pl.col("timestamp") < end_utc)

        return lazy.sort(["sparte", "messpunkt_id", "channel", "timestamp"]).collect()

    @staticmethod
    def _glob_pattern(*, sparte: str | None, year: int | None) -> str:
        sparte_part = f"sparte={sparte.lower()}" if sparte else "sparte=*"
        year_part = f"year={year}" if year is not None else "year=*"
        return f"{sparte_part}/{year_part}/*.parquet"
