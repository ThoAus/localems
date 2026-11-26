"""Standardized query, grouping, and export interface for LocalEMS analytics & raw data."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

import duckdb
import openpyxl
import polars as pl
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

from localems.config import LocalEmsConfig
from localems.db.repository import MasterDataRepository
from localems.storage import ParquetMeasurementStore

Period = Literal["day", "month", "quarter", "year", "total"]

_PERIOD_COLUMNS: dict[str, str | None] = {
    "day": "local_date",
    "month": "year_month",
    "quarter": "year_quarter",
    "year": "report_year",
    "total": None,
}

_CONSUMPTION_GROUP_ALIASES: dict[str, tuple[str, ...]] = {
    "energy_carrier": ("energy_carrier",),
    "carrier": ("energy_carrier",),
    "group": ('"group"',),
    "sub_group": ('"group"', "sub_group"),
    "object": ('"group"', "sub_group", "object_id", "object_name"),
    "object_id": ('"group"', "sub_group", "object_id", "object_name"),
    "allocation_type": ("allocation_type",),
}

_PV_GROUP_ALIASES: dict[str, tuple[str, ...]] = {
    "site": (
        "site_metering_point_id",
        "plant_mode",
        "object_id",
        "object_name",
        '"group"',
        "sub_group",
    ),
    "group": ('"group"',),
    "sub_group": ('"group"', "sub_group"),
    "object": ('"group"', "sub_group", "object_id", "object_name"),
    "object_id": ('"group"', "sub_group", "object_id", "object_name"),
    "plant_mode": ("plant_mode",),
}

_HEADER_FILL = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
_HEADER_FONT = Font(color="FFFFFF", bold=True)


def _normalize_str_values(value: str | Sequence[str] | None) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        return [value]
    items = [str(v) for v in value]
    return items if items else None


def _normalize_int_values(value: int | Sequence[int] | None) -> list[int] | None:
    if value is None:
        return None
    if isinstance(value, int):
        return [value]
    items = [int(v) for v in value]
    return items if items else None


def _parse_date_arg(value: date | str | None) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(value.strip())


def _parse_datetime_utc(value: datetime | date | str | None, timezone_name: str) -> datetime | None:
    if value is None:
        return None
    tz = ZoneInfo(timezone_name)
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt = datetime(value.year, value.month, value.day, 0, 0)
    else:
        text = value.strip()
        if len(text) == 10 and text[4] == "-" and text[7] == "-":
            d = date.fromisoformat(text)
            dt = datetime(d.year, d.month, d.day, 0, 0)
        else:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(UTC)


def _add_in_filter(
    clauses: list[str],
    params: list[Any],
    column_sql: str,
    values: list[Any] | None,
    *,
    lowercase: bool = False,
) -> None:
    if not values:
        return
    cleaned = [v.lower() if (lowercase and isinstance(v, str)) else v for v in values]
    placeholders = ", ".join("?" for _ in cleaned)
    if lowercase:
        clauses.append(f"lower({column_sql}) IN ({placeholders})")
    else:
        clauses.append(f"{column_sql} IN ({placeholders})")
    params.extend(cleaned)


def _dedupe_preserve_order(items: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def export_dataframe(
    df: pl.DataFrame,
    output_path: Path | str,
    *,
    sheet_name: str = "report",
) -> Path:
    """Export a Polars DataFrame to `.csv` or styled `.xlsx` (Excel / LibreOffice)."""
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    suffix = out.suffix.lower()

    if suffix == ".csv":
        df.write_csv(out)
        return out

    if suffix == ".xlsx":
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = sheet_name[:31]

        cols = df.columns
        for c_idx, col_name in enumerate(cols, start=1):
            cell = ws.cell(row=1, column=c_idx, value=col_name)
            cell.fill = _HEADER_FILL
            cell.font = _HEADER_FONT

        for r_idx, row in enumerate(df.iter_rows(), start=2):
            for c_idx, val in enumerate(row, start=1):
                # Excel does not support timezone-aware datetimes
                if isinstance(val, datetime) and val.tzinfo is not None:
                    val = val.isoformat()
                ws.cell(row=r_idx, column=c_idx, value=val)

        if cols:
            last_col = get_column_letter(len(cols))
            ws.freeze_panes = "A2"
            ws.auto_filter.ref = f"A1:{last_col}{max(df.height + 1, 2)}"
            for c_idx, col_name in enumerate(cols, start=1):
                col_letter = get_column_letter(c_idx)
                max_len = len(col_name)
                for r_idx in range(2, min(df.height + 2, 502)):
                    v = ws.cell(row=r_idx, column=c_idx).value
                    if v is not None:
                        max_len = max(max_len, len(str(v)))
                ws.column_dimensions[col_letter].width = min(max(max_len + 3, 12), 44)

        wb.save(out)
        wb.close()
        return out

    raise ValueError(f"Unsupported export format '{suffix}'. Supported: .csv, .xlsx")


class EnergyReporter:
    """Standardized query & rollup interface over LocalEMS DuckDB marts, SQLite, and Parquet."""

    def __init__(self, config: LocalEmsConfig | Path | str = "data") -> None:
        if isinstance(config, LocalEmsConfig):
            self.config = config
        else:
            self.config = LocalEmsConfig.from_env(config)

    def _connect_duckdb(self) -> duckdb.DuckDBPyConnection:
        db_path = self.config.duckdb_path
        if not db_path.exists():
            raise FileNotFoundError(
                f"Analytics database not found at '{db_path}'. "
                "Run 'localems dbt run' first to build the reporting tables."
            )
        return duckdb.connect(str(db_path), read_only=True)

    def _query_polars(self, sql: str, params: list[Any]) -> pl.DataFrame:
        with self._connect_duckdb() as con:
            rel = con.sql(sql, params=params) if params else con.sql(sql)
            table = (
                rel.to_arrow_table()
                if hasattr(rel, "to_arrow_table")
                else rel.fetch_arrow_table()
            )
        result = pl.from_arrow(table)
        return result if isinstance(result, pl.DataFrame) else pl.DataFrame()

    def consumption(
        self,
        *,
        period: Period = "month",
        group_by: Sequence[str] | str | None = ("object",),
        energy_carrier: str | Sequence[str] | None = None,
        group: str | Sequence[str] | None = None,
        sub_group: str | Sequence[str] | None = None,
        object_id: str | Sequence[str] | None = None,
        allocation_type: str | Sequence[str] | None = None,
        year: int | Sequence[int] | None = None,
        quarter: int | Sequence[int] | None = None,
        month: int | Sequence[int] | None = None,
        date_from: date | str | None = None,
        date_to: date | str | None = None,
        pivot: bool = False,
    ) -> pl.DataFrame:
        """Query allocated energy consumption at any time period and grouping level.

        Always preserves `energy_carrier` and `unit` so different carriers/units are never summed.
        """
        if period not in _PERIOD_COLUMNS:
            raise ValueError(
                f"Invalid period {period!r}. Allowed: {sorted(_PERIOD_COLUMNS)}"
            )
        if pivot and period == "total":
            raise ValueError("pivot=True cannot be combined with period='total'.")

        period_col = _PERIOD_COLUMNS[period]
        raw_dims = (
            [group_by]
            if isinstance(group_by, str)
            else list(group_by) if group_by is not None else []
        )

        dim_cols: list[str] = []
        if period_col is not None:
            dim_cols.append(period_col)
        dim_cols.append("energy_carrier")

        for token in raw_dims:
            key = token.strip().lower()
            if key not in _CONSUMPTION_GROUP_ALIASES:
                raise ValueError(
                    f"Unknown group_by dimension {token!r}. "
                    f"Allowed: {sorted(_CONSUMPTION_GROUP_ALIASES)}"
                )
            dim_cols.extend(_CONSUMPTION_GROUP_ALIASES[key])

        dim_cols.append("unit")
        group_cols = _dedupe_preserve_order(dim_cols)

        clauses: list[str] = []
        params: list[Any] = []
        _add_in_filter(
            clauses, params, "energy_carrier", _normalize_str_values(energy_carrier), lowercase=True
        )
        _add_in_filter(clauses, params, '"group"', _normalize_str_values(group))
        _add_in_filter(clauses, params, "sub_group", _normalize_str_values(sub_group))
        _add_in_filter(clauses, params, "object_id", _normalize_str_values(object_id))
        _add_in_filter(
            clauses,
            params,
            "allocation_type",
            _normalize_str_values(allocation_type),
            lowercase=True,
        )
        _add_in_filter(clauses, params, "report_year", _normalize_int_values(year))
        _add_in_filter(clauses, params, "report_quarter", _normalize_int_values(quarter))
        _add_in_filter(clauses, params, "report_month", _normalize_int_values(month))

        d_from = _parse_date_arg(date_from)
        d_to = _parse_date_arg(date_to)
        if d_from is not None:
            clauses.append("local_date >= ?")
            params.append(d_from)
        if d_to is not None:
            clauses.append("local_date <= ?")
            params.append(d_to)

        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        select_dims = ", ".join(group_cols)

        sql = f"""
            SELECT
                {select_dims},
                ROUND(SUM(total_consumption), 4) AS total_consumption,
                MAX(max_peak_power_kw) AS max_peak_power_kw,
                COUNT(DISTINCT local_date) AS active_days
            FROM mart_daily_consumption
            {where_sql}
            GROUP BY {select_dims}
            ORDER BY {select_dims}
        """

        df = self._query_polars(sql, params)

        if pivot and period_col is not None and not df.is_empty():
            index_cols = [c.strip('"') for c in group_cols if c != period_col]
            work = df.with_columns(pl.col(period_col).cast(pl.String).alias(period_col))
            period_order = sorted(work[period_col].unique().to_list())
            pivoted = work.pivot(
                values="total_consumption",
                index=index_cols,
                on=period_col,
                aggregate_function="sum",
            )
            ordered_cols = [*index_cols, *[p for p in period_order if p in pivoted.columns]]
            pivoted = pivoted.select(ordered_cols).sort(index_cols)
            value_cols = [p for p in period_order if p in pivoted.columns]
            pivoted = pivoted.with_columns(
                pl.sum_horizontal(value_cols).round(4).alias("total")
            )
            return pivoted

        return df

    def pv_summary(
        self,
        *,
        period: Period = "month",
        group_by: Sequence[str] | str | None = ("site",),
        site_metering_point_id: str | Sequence[str] | None = None,
        plant_mode: str | Sequence[str] | None = None,
        group: str | Sequence[str] | None = None,
        sub_group: str | Sequence[str] | None = None,
        object_id: str | Sequence[str] | None = None,
        year: int | Sequence[int] | None = None,
        quarter: int | Sequence[int] | None = None,
        month: int | Sequence[int] | None = None,
        date_from: date | str | None = None,
        date_to: date | str | None = None,
    ) -> pl.DataFrame:
        """Query PV production, feed-in, self-consumption, and autarky KPIs with exact ratios."""
        if period not in _PERIOD_COLUMNS:
            raise ValueError(
                f"Invalid period {period!r}. Allowed: {sorted(_PERIOD_COLUMNS)}"
            )

        period_col = _PERIOD_COLUMNS[period]
        raw_dims = (
            [group_by]
            if isinstance(group_by, str)
            else list(group_by) if group_by is not None else []
        )

        dim_cols: list[str] = []
        if period_col is not None:
            dim_cols.append(period_col)

        for token in raw_dims:
            key = token.strip().lower()
            if key not in _PV_GROUP_ALIASES:
                raise ValueError(
                    f"Unknown PV group_by dimension {token!r}. "
                    f"Allowed: {sorted(_PV_GROUP_ALIASES)}"
                )
            dim_cols.extend(_PV_GROUP_ALIASES[key])

        dim_cols.append("unit")
        group_cols = _dedupe_preserve_order(dim_cols)

        clauses: list[str] = []
        params: list[Any] = []
        _add_in_filter(
            clauses, params, "site_metering_point_id", _normalize_str_values(site_metering_point_id)
        )
        _add_in_filter(
            clauses, params, "plant_mode", _normalize_str_values(plant_mode), lowercase=True
        )
        _add_in_filter(clauses, params, '"group"', _normalize_str_values(group))
        _add_in_filter(clauses, params, "sub_group", _normalize_str_values(sub_group))
        _add_in_filter(clauses, params, "object_id", _normalize_str_values(object_id))
        _add_in_filter(clauses, params, "report_year", _normalize_int_values(year))
        _add_in_filter(clauses, params, "report_quarter", _normalize_int_values(quarter))
        _add_in_filter(clauses, params, "report_month", _normalize_int_values(month))

        d_from = _parse_date_arg(date_from)
        d_to = _parse_date_arg(date_to)
        if d_from is not None:
            clauses.append("local_date >= ?")
            params.append(d_from)
        if d_to is not None:
            clauses.append("local_date <= ?")
            params.append(d_to)

        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        select_dims = ", ".join(group_cols)

        sql = f"""
            SELECT
                {select_dims},
                ROUND(SUM(pv_production_kwh), 4) AS pv_production_kwh,
                ROUND(SUM(grid_export_kwh), 4) AS grid_export_kwh,
                ROUND(SUM(grid_import_kwh), 4) AS grid_import_kwh,
                ROUND(SUM(self_consumption_kwh), 4) AS self_consumption_kwh,
                ROUND(SUM(total_site_consumption_kwh), 4) AS total_site_consumption_kwh,
                ROUND(
                    100.0 * SUM(self_consumption_kwh) / NULLIF(SUM(pv_production_kwh), 0),
                    2
                ) AS self_consumption_rate_pct,
                ROUND(
                    100.0 * SUM(self_consumption_kwh) / NULLIF(SUM(total_site_consumption_kwh), 0),
                    2
                ) AS autarky_rate_pct,
                SUM(export_exceeds_production_intervals) AS export_exceeds_production_intervals,
                COUNT(DISTINCT local_date) AS active_days
            FROM mart_daily_pv_summary
            {where_sql}
            GROUP BY {select_dims}
            ORDER BY {select_dims}
        """

        return self._query_polars(sql, params)

    def plausibility(
        self,
        *,
        energy_carrier: str | Sequence[str] | None = None,
        group: str | Sequence[str] | None = None,
        sub_group: str | Sequence[str] | None = None,
        object_id: str | Sequence[str] | None = None,
        metering_point_id: str | Sequence[str] | None = None,
        year: int | Sequence[int] | None = None,
        quarter: int | Sequence[int] | None = None,
        month: int | Sequence[int] | None = None,
    ) -> pl.DataFrame:
        """Query the monthly data-quality, meter-tree, and residual plausibility scorecard."""
        clauses: list[str] = []
        params: list[Any] = []
        _add_in_filter(
            clauses, params, "energy_carrier", _normalize_str_values(energy_carrier), lowercase=True
        )
        _add_in_filter(clauses, params, '"group"', _normalize_str_values(group))
        _add_in_filter(clauses, params, "sub_group", _normalize_str_values(sub_group))
        _add_in_filter(clauses, params, "object_id", _normalize_str_values(object_id))
        _add_in_filter(
            clauses, params, "metering_point_id", _normalize_str_values(metering_point_id)
        )
        _add_in_filter(clauses, params, "report_year", _normalize_int_values(year))
        _add_in_filter(clauses, params, "report_quarter", _normalize_int_values(quarter))
        _add_in_filter(clauses, params, "report_month", _normalize_int_values(month))

        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"""
            SELECT *
            FROM mart_plausibility_summary
            {where_sql}
            ORDER BY year_month, energy_carrier, tree_path
        """

        return self._query_polars(sql, params)

    def intervals(
        self,
        *,
        metering_point_ids: str | Sequence[str] | None = None,
        energy_carrier: str | None = None,
        channel: str | Sequence[str] | None = None,
        year: int | None = None,
        start: datetime | date | str | None = None,
        end: datetime | date | str | None = None,
        local_time: bool = True,
    ) -> pl.DataFrame:
        """Query raw 15-minute (or manual) measurements directly from Parquet for 1+ meters."""
        mp_list = _normalize_str_values(metering_point_ids)
        ch_list = _normalize_str_values(channel)
        start_utc = _parse_datetime_utc(start, self.config.default_timezone)
        end_utc = _parse_datetime_utc(end, self.config.default_timezone)

        store = ParquetMeasurementStore(
            self.config.parquet_dir,
            compression=self.config.parquet_compression,
            default_timezone=self.config.default_timezone,
        )
        df = store.read(
            energy_carrier=energy_carrier,
            year=year,
            metering_point_ids=mp_list,
            start_utc=start_utc,
            end_utc=end_utc,
        )
        if ch_list and not df.is_empty():
            df = df.filter(pl.col("channel").is_in([c.lower() for c in ch_list]))

        if local_time and not df.is_empty():
            df = df.with_columns(
                pl.col("timestamp")
                .dt.convert_time_zone(self.config.default_timezone)
                .alias("timestamp_local")
            )
            cols = ["timestamp", "timestamp_local"] + [
                c for c in df.columns if c not in ("timestamp", "timestamp_local")
            ]
            df = df.select(cols)

        return df

    def groups(self) -> pl.DataFrame:
        """List all distinct (group, sub_group) combinations and object counts from SQLite."""
        repo = MasterDataRepository(self.config.db_path, backups_dir=self.config.backups_dir)
        rows = repo.fetch_table("objects")
        if not rows:
            return pl.DataFrame(
                schema={
                    "group": pl.String,
                    "sub_group": pl.String,
                    "object_count": pl.UInt32,
                    "active_objects": pl.Int64,
                }
            )
        return (
            pl.DataFrame(rows)
            .group_by(["group", "sub_group"])
            .agg(
                pl.len().alias("object_count"),
                pl.col("active").sum().alias("active_objects"),
            )
            .sort(["group", "sub_group"])
        )

    def objects(self, *, active_only: bool = True) -> pl.DataFrame:
        """Return master-data `objects` from SQLite as a Polars DataFrame."""
        repo = MasterDataRepository(self.config.db_path, backups_dir=self.config.backups_dir)
        rows = repo.fetch_table("objects")
        if not rows:
            return pl.DataFrame()
        df = pl.DataFrame(rows)
        return df.filter(pl.col("active") == 1) if active_only else df

    def metering_points(self, *, active_only: bool = True) -> pl.DataFrame:
        """Return master-data `metering_points` from SQLite as a Polars DataFrame."""
        repo = MasterDataRepository(self.config.db_path, backups_dir=self.config.backups_dir)
        rows = repo.fetch_table("metering_points")
        if not rows:
            return pl.DataFrame()
        df = pl.DataFrame(rows)
        return df.filter(pl.col("active") == 1) if active_only else df
