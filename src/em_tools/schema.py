"""Canonical measurement schema and validation helpers using Polars and PyArrow."""

from __future__ import annotations

from datetime import UTC, datetime

import polars as pl
import pyarrow as pa

ALLOWED_ENERGY_CARRIERS = frozenset({"electricity", "water", "gas", "heating", "other"})
ALLOWED_CHANNELS = frozenset({"consumption", "grid_import", "grid_export", "pv_production"})
ALLOWED_READING_TYPES = frozenset({"interval", "cumulative", "instantaneous"})
ALLOWED_QUALITIES = frozenset(
    {"measured", "manual", "estimated", "interpolated", "suspect"}
)

DEDUP_KEY_COLUMNS: tuple[str, ...] = (
    "metering_point_id",
    "energy_carrier",
    "channel",
    "timestamp",
    "reading_type",
)

MEASUREMENT_POLARS_SCHEMA: dict[str, pl.DataType] = {
    "timestamp": pl.Datetime("us", "UTC"),
    "interval_minutes": pl.Int16,
    "metering_point_id": pl.String,
    "object_id": pl.String,
    "energy_carrier": pl.String,
    "channel": pl.String,
    "value": pl.Float64,
    "unit": pl.String,
    "reading_type": pl.String,
    "quality": pl.String,
    "peak_power_kw": pl.Float64,
    "flow_rate": pl.Float64,
    "pressure_bar": pl.Float64,
    "burn_duration_min": pl.Float64,
    "source": pl.String,
    "ingested_at": pl.Datetime("us", "UTC"),
}

MEASUREMENT_ARROW_SCHEMA: pa.Schema = pa.schema(
    [
        pa.field("timestamp", pa.timestamp("us", tz="UTC"), nullable=False),
        pa.field("interval_minutes", pa.int16(), nullable=True),
        pa.field("metering_point_id", pa.string(), nullable=False),
        pa.field("object_id", pa.string(), nullable=True),
        pa.field("energy_carrier", pa.string(), nullable=False),
        pa.field("channel", pa.string(), nullable=False),
        pa.field("value", pa.float64(), nullable=False),
        pa.field("unit", pa.string(), nullable=False),
        pa.field("reading_type", pa.string(), nullable=False),
        pa.field("quality", pa.string(), nullable=False),
        pa.field("peak_power_kw", pa.float64(), nullable=True),
        pa.field("flow_rate", pa.float64(), nullable=True),
        pa.field("pressure_bar", pa.float64(), nullable=True),
        pa.field("burn_duration_min", pa.float64(), nullable=True),
        pa.field("source", pa.string(), nullable=False),
        pa.field("ingested_at", pa.timestamp("us", tz="UTC"), nullable=False),
    ]
)

REQUIRED_NON_NULL_COLUMNS: tuple[str, ...] = (
    "timestamp",
    "metering_point_id",
    "energy_carrier",
    "channel",
    "value",
    "unit",
    "reading_type",
    "quality",
    "source",
    "ingested_at",
)

OPTIONAL_NULLABLE_COLUMNS: tuple[str, ...] = (
    "interval_minutes",
    "object_id",
    "peak_power_kw",
    "flow_rate",
    "pressure_bar",
    "burn_duration_min",
)


class SchemaValidationError(ValueError):
    """Raised when a measurements DataFrame violates the canonical schema."""


def normalize_measurements(
    df: pl.DataFrame,
    *,
    default_timezone: str = "Europe/Rome",
    ingested_at: datetime | None = None,
) -> pl.DataFrame:
    """Conform an input DataFrame to the canonical measurements schema and validate it.

    - Adds missing optional nullable columns as nulls with canonical types.
    - Defaults `ingested_at` to current UTC time if missing.
    - Converts naive `timestamp` values using `default_timezone` (DST-aware) to UTC.
    - Casts all columns to canonical Polars types and validates non-null & enum constraints.
    """
    if df.is_empty():
        return pl.DataFrame(schema=MEASUREMENT_POLARS_SCHEMA)

    work = df.clone()
    now_utc = ingested_at or datetime.now(UTC)

    if "ingested_at" not in work.columns:
        work = work.with_columns(
            pl.lit(now_utc).cast(pl.Datetime("us", "UTC")).alias("ingested_at")
        )
    if "channel" not in work.columns:
        work = work.with_columns(pl.lit("consumption").alias("channel"))
    if "reading_type" not in work.columns:
        work = work.with_columns(pl.lit("interval").alias("reading_type"))
    if "quality" not in work.columns:
        work = work.with_columns(pl.lit("measured").alias("quality"))

    for col in OPTIONAL_NULLABLE_COLUMNS:
        if col not in work.columns:
            work = work.with_columns(
                pl.lit(None, dtype=MEASUREMENT_POLARS_SCHEMA[col]).alias(col)
            )

    missing_required = [
        c for c in REQUIRED_NON_NULL_COLUMNS if c not in work.columns
    ]
    if missing_required:
        raise SchemaValidationError(
            f"Missing required measurement columns: {', '.join(missing_required)}"
        )

    # Normalize timestamp to UTC (us precision)
    ts_dtype = work.schema["timestamp"]
    if isinstance(ts_dtype, pl.String):
        work = work.with_columns(
            pl.col("timestamp").str.to_datetime(time_unit="us")
        )
        ts_dtype = work.schema["timestamp"]

    if isinstance(ts_dtype, pl.Datetime):
        if ts_dtype.time_zone is None:
            work = work.with_columns(
                pl.col("timestamp")
                .dt.replace_time_zone(
                    default_timezone, ambiguous="earliest", non_existent="raise"
                )
                .dt.convert_time_zone("UTC")
                .dt.cast_time_unit("us")
            )
        else:
            work = work.with_columns(
                pl.col("timestamp").dt.convert_time_zone("UTC").dt.cast_time_unit("us")
            )
    else:
        raise SchemaValidationError(
            f"Column 'timestamp' must be Datetime or ISO string, got {ts_dtype}"
        )

    # Normalize ingested_at to UTC (us precision)
    ing_dtype = work.schema["ingested_at"]
    if isinstance(ing_dtype, pl.Datetime) and ing_dtype.time_zone is None:
        work = work.with_columns(
            pl.col("ingested_at")
            .dt.replace_time_zone("UTC")
            .dt.cast_time_unit("us")
        )

    # Select & cast exact canonical column order
    cast_exprs = [
        pl.col(col).cast(dtype) for col, dtype in MEASUREMENT_POLARS_SCHEMA.items()
    ]
    work = work.select(cast_exprs)

    # Normalize enum & identifier strings
    work = work.with_columns(
        pl.col("energy_carrier").str.strip_chars().str.to_lowercase(),
        pl.col("channel").str.strip_chars().str.to_lowercase(),
        pl.col("reading_type").str.strip_chars().str.to_lowercase(),
        pl.col("quality").str.strip_chars().str.to_lowercase(),
        pl.col("metering_point_id").str.strip_chars(),
    )

    validate_measurements(work)
    return work


def validate_measurements(df: pl.DataFrame) -> None:
    """Validate canonical column presence, non-nulls, and allowed domain values."""
    expected_cols = list(MEASUREMENT_POLARS_SCHEMA.keys())
    if df.columns != expected_cols:
        raise SchemaValidationError(
            f"Columns mismatch. Expected {expected_cols}, got {df.columns}"
        )

    for col in REQUIRED_NON_NULL_COLUMNS:
        null_count = df[col].null_count()
        if null_count > 0:
            raise SchemaValidationError(
                f"Required column '{col}' contains {null_count} null value(s)."
            )

    invalid_carriers = set(df["energy_carrier"].unique().to_list()) - ALLOWED_ENERGY_CARRIERS
    if invalid_carriers:
        raise SchemaValidationError(
            f"Invalid energy_carrier value(s): {sorted(invalid_carriers)}. "
            f"Allowed: {sorted(ALLOWED_ENERGY_CARRIERS)}"
        )

    invalid_channels = set(df["channel"].unique().to_list()) - ALLOWED_CHANNELS
    if invalid_channels:
        raise SchemaValidationError(
            f"Invalid channel value(s): {sorted(invalid_channels)}. "
            f"Allowed: {sorted(ALLOWED_CHANNELS)}"
        )

    invalid_rtypes = set(df["reading_type"].unique().to_list()) - ALLOWED_READING_TYPES
    if invalid_rtypes:
        raise SchemaValidationError(
            f"Invalid reading_type value(s): {sorted(invalid_rtypes)}. "
            f"Allowed: {sorted(ALLOWED_READING_TYPES)}"
        )

    invalid_qualities = set(df["quality"].unique().to_list()) - ALLOWED_QUALITIES
    if invalid_qualities:
        raise SchemaValidationError(
            f"Invalid quality value(s): {sorted(invalid_qualities)}. "
            f"Allowed: {sorted(ALLOWED_QUALITIES)}"
        )
