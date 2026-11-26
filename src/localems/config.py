"""Runtime configuration and directory paths for LocalEMS."""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, Field


class LocalEmsConfig(BaseModel):
    """Centralized path and behavior configuration for a LocalEMS workspace."""

    data_dir: Path = Field(default=Path("data"))
    default_timezone: str = Field(
        default="Europe/Rome",
        description="Default local timezone assumed for naive timestamps in raw files.",
    )
    parquet_compression: str = Field(default="zstd")
    duckdb_extensions_dir: Path | None = Field(
        default=None,
        description=(
            "Optional directory for DuckDB extensions (defaults to <data_dir>/.duckdb_extensions "
            "or LOCALEMS_DUCKDB_EXT_DIR)."
        ),
    )

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def archive_dir(self) -> Path:
        return self.data_dir / "archive"

    @property
    def parquet_dir(self) -> Path:
        return self.data_dir / "parquet"

    @property
    def parquet_glob(self) -> str:
        return (
            self.parquet_dir.resolve() / "energy_carrier=*" / "year=*" / "*.parquet"
        ).as_posix()

    @property
    def db_dir(self) -> Path:
        return self.data_dir / "db"

    @property
    def db_path(self) -> Path:
        return self.db_dir / "masterdata.sqlite"

    @property
    def backups_dir(self) -> Path:
        return self.db_dir / "backups"

    @property
    def editor_dir(self) -> Path:
        return self.data_dir / "editor"

    @property
    def default_editor_path(self) -> Path:
        return self.editor_dir / "masterdata_roundtrip.xlsx"

    @property
    def duckdb_path(self) -> Path:
        return self.data_dir / "localems.duckdb"

    @property
    def dbt_runtime_dir(self) -> Path:
        return self.data_dir / ".dbt"

    @property
    def dbt_target_dir(self) -> Path:
        return self.dbt_runtime_dir / "target"

    @property
    def dbt_logs_dir(self) -> Path:
        return self.dbt_runtime_dir / "logs"

    @property
    def resolved_duckdb_extensions_dir(self) -> Path:
        return self.duckdb_extensions_dir or (self.data_dir / ".duckdb_extensions")

    def ensure_directories(self) -> None:
        """Create standard local workspace directories if they do not exist."""
        for path in (
            self.raw_dir,
            self.archive_dir,
            self.parquet_dir,
            self.db_dir,
            self.backups_dir,
            self.editor_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_env(cls, data_dir: Path | str | None = None) -> LocalEmsConfig:
        """Build configuration from explicit override or LOCALEMS_DATA_DIR env var."""
        if data_dir is not None:
            resolved = Path(data_dir)
        else:
            resolved = Path(os.environ.get("LOCALEMS_DATA_DIR", "data"))
        tz = os.environ.get("LOCALEMS_TIMEZONE", "Europe/Rome")
        ext_env = os.environ.get("LOCALEMS_DUCKDB_EXT_DIR")
        ext_dir = Path(ext_env) if ext_env else None
        return cls(data_dir=resolved, default_timezone=tz, duckdb_extensions_dir=ext_dir)
