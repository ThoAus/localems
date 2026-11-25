"""Reader for Edyna 15-minute electricity smart-meter .xlsx exports (96 columns/day)."""

from __future__ import annotations

import itertools
from datetime import date, datetime
from pathlib import Path
from typing import Any

import openpyxl
import polars as pl

from em_tools.readers.base import BaseReader
from em_tools.schema import normalize_measurements

QUARTER_HOUR_SLOTS: tuple[str, ...] = tuple(
    f"{h:02d}{m:02d}" for h, m in itertools.product(range(24), (0, 15, 30, 45))
)


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


class EdynaXlsxReader(BaseReader):
    """Reads Edyna smart-meter Excel exports where the sheet name is the POD identifier.

    Expected raw file layout (wide format):
      - First sheet name = POD (`metering_point_id`, unless overridden via kwarg)
      - Column 1 (provider header `GIORNO`): date (`YYYYMMDD`, `dd/mm/yyyy`, or Excel date)
      - Next 96 columns: 15-minute interval values (`0000` .. `2345`)
      - Optional trailing summary columns (ignored beyond the 96 interval columns)
    """

    name = "edyna_xlsx"
    description = "Edyna 15-minute electricity .xlsx export (date + 96 quarter-hour columns)."

    def read(
        self,
        path: Path | str,
        *,
        default_timezone: str = "Europe/Rome",
        energy_carrier: str = "electricity",
        unit: str = "kWh",
        channel: str = "consumption",
        metering_point_id: str | None = None,
        object_id: str | None = None,
        source: str | None = None,
        **_: Any,
    ) -> pl.DataFrame:
        file_path = Path(path)
        wb = openpyxl.load_workbook(file_path, data_only=True, read_only=True)
        try:
            sheet_name = wb.sheetnames[0]
            ws = wb[sheet_name]
            pod = (metering_point_id or sheet_name).strip()

            rows_iter = ws.iter_rows(values_only=True)
            header = next(rows_iter, None)
            if header is None or len(header) < 97:
                raise ValueError(
                    f"Edyna file '{file_path.name}' must have at least 97 columns "
                    f"(date column + 96 15-minute intervals)."
                )

            records: list[dict[str, Any]] = []
            src_label = source or f"{self.name}:{file_path.name}"

            for raw_row in rows_iter:
                if not raw_row or raw_row[0] is None:
                    continue
                day = _parse_date_cell(raw_row[0])
                interval_vals = raw_row[1:97]
                for slot, raw_val in zip(QUARTER_HOUR_SLOTS, interval_vals, strict=False):
                    num = _parse_float(raw_val)
                    if num is None:
                        continue
                    hour = int(slot[:2])
                    minute = int(slot[2:])
                    ts_local = datetime(day.year, day.month, day.day, hour, minute)
                    records.append(
                        {
                            "timestamp": ts_local,
                            "interval_minutes": 15,
                            "metering_point_id": pod,
                            "object_id": object_id,
                            "energy_carrier": energy_carrier,
                            "channel": channel,
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
