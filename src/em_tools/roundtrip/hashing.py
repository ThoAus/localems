"""Canonical cell normalization and deterministic row hashing for roundtrip change detection."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from typing import Any

from em_tools.db.repository import TABLE_COLUMNS

HASH_COLUMN = "_row_hash"

INT_COLUMNS = frozenset({"rule_id", "active"})
FLOAT_COLUMNS = frozenset({"factor"})
DATE_COLUMNS = frozenset({"valid_from", "valid_to"})
LOWER_ENUM_COLUMNS = frozenset({"energy_carrier", "meter_type"})


def normalize_cell(column: str, value: Any) -> Any:
    """Normalize a raw cell value from SQLite or openpyxl into a canonical Python scalar."""
    if value is None:
        return None

    if isinstance(value, str):
        cleaned = value.strip()
        if cleaned == "":
            return None
        if column in INT_COLUMNS:
            if cleaned.lower() in {"true", "yes"}:
                return 1
            if cleaned.lower() in {"false", "no"}:
                return 0
            try:
                num = float(cleaned)
                if num.is_integer():
                    return int(num)
            except ValueError:
                return cleaned
            return cleaned
        if column in FLOAT_COLUMNS:
            try:
                return round(float(cleaned.replace(",", ".")), 8)
            except ValueError:
                return cleaned
        if column in LOWER_ENUM_COLUMNS:
            return cleaned.lower()
        return cleaned

    if isinstance(value, bool):
        return int(value) if column in INT_COLUMNS else value

    if isinstance(value, datetime):
        if column in DATE_COLUMNS:
            return value.date().isoformat()
        return value.isoformat()

    if isinstance(value, date):
        return value.isoformat()

    if isinstance(value, (int, float)):
        if column in INT_COLUMNS:
            if isinstance(value, float) and not value.is_integer():
                return value
            return int(value)
        if column in FLOAT_COLUMNS:
            return round(float(value), 8)
        return str(int(value)) if isinstance(value, float) and value.is_integer() else str(value)

    return str(value).strip() or None


def normalize_row(table: str, raw_row: dict[str, Any]) -> dict[str, Any]:
    """Normalize all managed columns of a row dict for `table`."""
    cols = TABLE_COLUMNS[table]
    normalized: dict[str, Any] = {}
    for col in cols:
        val = normalize_cell(col, raw_row.get(col))
        if col == "active" and val is None:
            val = 1
        normalized[col] = val
    return normalized


def compute_row_hash(table: str, row: dict[str, Any]) -> str:
    """Compute a deterministic 16-hex-char SHA-256 hash over a normalized row."""
    norm = normalize_row(table, row)
    payload = json.dumps(norm, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
