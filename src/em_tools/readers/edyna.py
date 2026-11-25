"""Reader for Edyna 15-minute electricity .xlsx exports with EU DST (23h/24h/25h) handling.

Supported Edyna filename conventions (case-insensitive):
  - `ATTIVA_<POD>_<YYYYMM>.xlsx`    -> channel = `grid_import`,   external_key = `<POD>`
  - `ATTIVA_S_<POD>_<YYYYMM>.xlsx`  -> channel = `grid_export`,   external_key = `<POD>`
  - `ATTIVA_<POD>P_<YYYYMM>.xlsx`   -> channel = `pv_production`, external_key = `<POD>P`
                                       (base_external_key = `<POD>`)

Column layout (local time, default `Europe/Rome`):
  - Column 0 (`GIORNO`): date (`YYYYMMDD`, `dd/mm/yyyy`, ISO `YYYY-MM-DD`, or Excel date)
  - Columns 1..96 (`Ora 1 00 - 01 I° Q` .. `Ora 24 23 - 24 IV° Q`): 15-minute interval starts
  - Columns 97..100 (`Ora 25 02 - 03 I° Q` .. `IV° Q`): extra DST fall-back hour (25h day only!)
"""

from __future__ import annotations

import itertools
import re
import warnings
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import openpyxl
import polars as pl

from em_tools.db.repository import MasterDataRepository
from em_tools.readers.base import BaseReader
from em_tools.schema import normalize_measurements

QUARTER_HOUR_SLOTS: tuple[tuple[int, int], ...] = tuple(
    (h, m) for h, m in itertools.product(range(24), (0, 15, 30, 45))
)
DST_EXTRA_HOUR_SLOTS: tuple[tuple[int, int], ...] = ((2, 0), (2, 15), (2, 30), (2, 45))

_EDYNA_EXPORT_RE = re.compile(
    r"^ATTIVA_S_(?P<pod>[A-Z0-9]+)_(?P<ym>\d{6})\.xlsx$", re.IGNORECASE
)
_EDYNA_PV_RE = re.compile(
    r"^ATTIVA_(?P<pod>[A-Z0-9]+P)_(?P<ym>\d{6})\.xlsx$", re.IGNORECASE
)
_EDYNA_IMPORT_RE = re.compile(
    r"^ATTIVA_(?P<pod>[A-Z0-9]+)_(?P<ym>\d{6})\.xlsx$", re.IGNORECASE
)


@dataclass(frozen=True)
class EdynaFileMeta:
    """Metadata parsed from an Edyna ATTIVA_*.xlsx filename."""

    channel: str
    external_key: str
    base_external_key: str
    year_month: str


def parse_edyna_filename(filename: str) -> EdynaFileMeta | None:
    """Parse channel and external meter key from a standard Edyna ATTIVA_*.xlsx filename."""
    m = _EDYNA_EXPORT_RE.match(filename)
    if m:
        pod = m.group("pod").upper()
        return EdynaFileMeta(
            channel="grid_export",
            external_key=pod,
            base_external_key=pod,
            year_month=m.group("ym"),
        )

    m = _EDYNA_PV_RE.match(filename)
    if m:
        pod_p = m.group("pod").upper()
        return EdynaFileMeta(
            channel="pv_production",
            external_key=pod_p,
            base_external_key=pod_p[:-1],
            year_month=m.group("ym"),
        )

    m = _EDYNA_IMPORT_RE.match(filename)
    if m:
        pod = m.group("pod").upper()
        return EdynaFileMeta(
            channel="grid_import",
            external_key=pod,
            base_external_key=pod,
            year_month=m.group("ym"),
        )

    return None


def _parse_date_cell(val: Any) -> date:
    if isinstance(val, datetime):
        return val.date()
    if isinstance(val, date):
        return val
    text = str(val).strip()
    if "/" in text:
        parts = text.split("/")
        if len(parts) == 3:
            day_s, month_s, year_s = parts
            return date(int(year_s), int(month_s), int(day_s))
    if len(text) == 8 and text.isdigit():
        return date(int(text[0:4]), int(text[4:6]), int(text[6:8]))
    return date.fromisoformat(text)


def _parse_float(val: Any) -> float | None:
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    text = str(val).strip()
    if not text:
        return None
    return float(text.replace(",", "."))


def _local_to_utc_if_exists(
    day: date, hour: int, minute: int, tz: ZoneInfo, *, fold: int = 0
) -> datetime | None:
    """Convert a local wall-clock time to UTC, returning None if nonexistent (spring-forward)."""
    naive = datetime(day.year, day.month, day.day, hour, minute)
    localized = naive.replace(tzinfo=tz, fold=fold)
    utc_dt = localized.astimezone(UTC)
    roundtrip = utc_dt.astimezone(tz).replace(tzinfo=None)
    if roundtrip != naive:
        return None
    return utc_dt


def _is_fallback_dst_day(day: date, tz: ZoneInfo) -> bool:
    """Return True if `day` has a repeated 02:00 hour (25-hour fall-back DST transition)."""
    t0 = datetime(day.year, day.month, day.day, 2, 0, tzinfo=tz, fold=0)
    t1 = datetime(day.year, day.month, day.day, 2, 0, tzinfo=tz, fold=1)
    return t0.utcoffset() != t1.utcoffset()


class EdynaXlsxReader(BaseReader):
    """Reads Edyna 15-minute electricity `.xlsx` exports (grid import, grid export, PV).

    Automatically detects `channel` (`grid_import`, `grid_export`, `pv_production`) and
    external meter key from `ATTIVA_*.xlsx` filenames, resolves `metering_point_id` via
    SQLite `external_id` if `db_path` is provided, and handles EU summer/winter DST days
    accurately (92 / 96 / 100 quarter-hour UTC intervals per day).
    """

    name = "edyna_xlsx"
    description = (
        "Edyna 15-min electricity .xlsx (ATTIVA_<POD>_YYYYMM / ATTIVA_S_<POD>_YYYYMM / "
        "ATTIVA_<POD>P_YYYYMM) with EU DST support."
    )

    def can_read(self, path: Path) -> bool:
        return parse_edyna_filename(path.name) is not None

    def read(
        self,
        path: Path | str,
        *,
        default_timezone: str = "Europe/Rome",
        energy_carrier: str = "electricity",
        unit: str = "kWh",
        channel: str | None = None,
        metering_point_id: str | None = None,
        object_id: str | None = None,
        db_path: Path | str | None = None,
        source: str | None = None,
        **_: Any,
    ) -> pl.DataFrame:
        file_path = Path(path)
        meta = parse_edyna_filename(file_path.name)
        tz = ZoneInfo(default_timezone)

        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message="Workbook contains no default style")
            wb = openpyxl.load_workbook(file_path, data_only=True, read_only=True)

        try:
            sheet_name = wb.sheetnames[0]
            ws = wb[sheet_name]

            resolved_channel = channel or (meta.channel if meta else "consumption")
            external_key = meta.external_key if meta else sheet_name.strip()
            base_external_key = meta.base_external_key if meta else external_key

            if metering_point_id is not None:
                resolved_mp_id = metering_point_id.strip()
            else:
                mapped: str | None = None
                if db_path is not None:
                    repo = MasterDataRepository(db_path)
                    mapped = repo.resolve_metering_point_id(
                        external_key,
                        channel=resolved_channel,
                        base_external_id=base_external_key,
                    )
                resolved_mp_id = mapped or external_key

            rows_iter = ws.iter_rows(values_only=True)
            header = next(rows_iter, None)
            if header is None or len(header) < 97:
                raise ValueError(
                    f"Edyna file '{file_path.name}' must have at least 97 columns "
                    f"(date column + 96 15-minute intervals)."
                )

            has_ora_25_cols = len(header) >= 101 and any(
                h is not None and "25" in str(h) for h in header[97:101]
            )

            records: list[dict[str, Any]] = []
            src_label = source or f"{self.name}:{file_path.name}"

            for raw_row in rows_iter:
                if not raw_row or raw_row[0] is None or str(raw_row[0]).strip() == "":
                    continue
                day = _parse_date_cell(raw_row[0])

                # Standard 96 quarter-hour slots (Ora 1 .. Ora 24, fold=0)
                interval_vals = raw_row[1:97]
                for (hour, minute), raw_val in zip(
                    QUARTER_HOUR_SLOTS, interval_vals, strict=False
                ):
                    ts_utc = _local_to_utc_if_exists(day, hour, minute, tz, fold=0)
                    if ts_utc is None:
                        # Spring-forward 23h DST day: 02:00..02:45 does not exist
                        continue
                    num = _parse_float(raw_val)
                    if num is None:
                        continue
                    records.append(
                        {
                            "timestamp": ts_utc,
                            "interval_minutes": 15,
                            "metering_point_id": resolved_mp_id,
                            "object_id": object_id,
                            "energy_carrier": energy_carrier,
                            "channel": resolved_channel,
                            "value": num,
                            "unit": unit,
                            "reading_type": "interval",
                            "quality": "measured",
                            "source": src_label,
                        }
                    )

                # Extra 4 slots (Ora 25: 02:00..02:45, fold=1) ONLY on 25h fall-back day
                if has_ora_25_cols and _is_fallback_dst_day(day, tz):
                    extra_vals = raw_row[97:101]
                    for (hour, minute), raw_val in zip(
                        DST_EXTRA_HOUR_SLOTS, extra_vals, strict=False
                    ):
                        num = _parse_float(raw_val)
                        if num is None:
                            continue
                        ts_utc = _local_to_utc_if_exists(day, hour, minute, tz, fold=1)
                        if ts_utc is None:
                            continue
                        records.append(
                            {
                                "timestamp": ts_utc,
                                "interval_minutes": 15,
                                "metering_point_id": resolved_mp_id,
                                "object_id": object_id,
                                "energy_carrier": energy_carrier,
                                "channel": resolved_channel,
                                "value": num,
                                "unit": unit,
                                "reading_type": "interval",
                                "quality": "measured",
                                "source": src_label,
                            }
                        )
        finally:
            wb.close()

        if not records:
            return normalize_measurements(pl.DataFrame(), default_timezone=default_timezone)

        df = pl.DataFrame(records)
        return normalize_measurements(df, default_timezone=default_timezone)
