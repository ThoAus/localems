"""Standard CSV reader for generic 15-minute, sub-meter, and manual energy readings."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import polars as pl

from em_tools.readers.base import BaseReader
from em_tools.schema import normalize_measurements


class StandardCsvReader(BaseReader):
    """Reads tidy CSV files (comma or semicolon separated) into the canonical schema.

    Expected minimum columns (or supplied via kwargs):
      - `timestamp`
      - `value`
      - `metering_point_id` (or kwarg `metering_point_id`)
      - `energy_carrier` (or kwarg `energy_carrier`)
      - `unit` (or kwarg `unit`)
    Optional columns like `object_id`, `interval_minutes`, `channel`, `reading_type`,
    `quality`, `peak_power_kw`, `flow_rate`, `pressure_bar`, `burn_duration_min` are
    preserved automatically when present.
    """

    name = "csv_standard"
    description = "Generic tidy CSV reader (smart meters, sub-meters, water, gas, manual)."

    def read(
        self,
        path: Path | str,
        *,
        separator: str = ",",
        decimal_comma: bool = False,
        default_timezone: str = "Europe/Rome",
        metering_point_id: str | None = None,
        energy_carrier: str | None = None,
        unit: str | None = None,
        interval_minutes: int | None = 15,
        reading_type: str | None = None,
        quality: str | None = None,
        source: str | None = None,
        **_: Any,
    ) -> pl.DataFrame:
        file_path = Path(path)
        df = pl.read_csv(
            file_path,
            separator=separator,
            decimal_comma=decimal_comma,
            try_parse_dates=True,
        )

        additions: list[pl.Expr] = []
        if "metering_point_id" not in df.columns and metering_point_id is not None:
            additions.append(pl.lit(metering_point_id).alias("metering_point_id"))
        if "energy_carrier" not in df.columns and energy_carrier is not None:
            additions.append(pl.lit(energy_carrier).alias("energy_carrier"))
        if "unit" not in df.columns and unit is not None:
            additions.append(pl.lit(unit).alias("unit"))
        if "interval_minutes" not in df.columns and interval_minutes is not None:
            additions.append(pl.lit(interval_minutes, dtype=pl.Int16).alias("interval_minutes"))
        if "reading_type" not in df.columns and reading_type is not None:
            additions.append(pl.lit(reading_type).alias("reading_type"))
        if "quality" not in df.columns and quality is not None:
            additions.append(pl.lit(quality).alias("quality"))
        if "source" not in df.columns:
            additions.append(pl.lit(source or f"{self.name}:{file_path.name}").alias("source"))

        if additions:
            df = df.with_columns(additions)

        return normalize_measurements(df, default_timezone=default_timezone)
