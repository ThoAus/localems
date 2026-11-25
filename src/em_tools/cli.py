"""Command-line interface for em_tools (init, ingest, export-editor, import-editor)."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from em_tools.config import EmToolsConfig
from em_tools.db.repository import MasterDataRepository
from em_tools.ingest import ingest_directory, ingest_file
from em_tools.readers.registry import DEFAULT_REGISTRY
from em_tools.roundtrip import RoundtripValidationError, export_to_editor, import_from_editor


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="em-tools",
        description=(
            "Energy consumption ingestion (Parquet), SQLite master-data roundtrip (.xlsx), "
            "and quarterly reporting toolkit."
        ),
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="Workspace data directory (default: EM_TOOLS_DATA_DIR or ./data).",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    # init
    sub.add_parser("init", help="Initialize data directories and SQLite master-data database.")

    # export-editor
    p_exp = sub.add_parser(
        "export-editor",
        help="Export SQLite master data into a formatted .xlsx workbook for editing.",
    )
    p_exp.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="Output .xlsx path (default: <data-dir>/editor/masterdata_roundtrip.xlsx).",
    )

    # import-editor
    p_imp = sub.add_parser(
        "import-editor",
        help="Validate and import an edited .xlsx master-data workbook into SQLite.",
    )
    p_imp.add_argument("input_xlsx", type=Path, help="Path to the edited .xlsx workbook.")
    p_imp.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and print diff without backing up or modifying SQLite.",
    )

    # ingest (supports single file OR directory with automatic filename-based reader detection)
    p_ing = sub.add_parser(
        "ingest",
        help="Read a file or folder (auto-detecting reader by filename) and upsert into Parquet.",
    )
    p_ing.add_argument(
        "input_path",
        type=Path,
        help="Path to a raw measurement file or a directory of raw files.",
    )
    p_ing.add_argument(
        "--reader",
        default=None,
        help="Optional reader name (omit to auto-detect from filename & extension).",
    )
    p_ing.add_argument(
        "--recursive",
        action="store_true",
        help="Recursively scan subdirectories when `input_path` is a directory.",
    )
    p_ing.add_argument(
        "--energy-carrier",
        default=None,
        help="Override/default energy_carrier (e.g. electricity, water, gas, heating).",
    )
    p_ing.add_argument(
        "--channel",
        default=None,
        help="Override/default channel (consumption, grid_import, grid_export, pv_production).",
    )
    p_ing.add_argument("--unit", default=None, help="Override/default unit (e.g. kWh, m3).")
    p_ing.add_argument(
        "--metering-point-id", default=None, help="Override/default metering_point_id."
    )
    p_ing.add_argument("--object-id", default=None, help="Optional object_id.")
    p_ing.add_argument(
        "--separator",
        default=",",
        help="CSV field separator for csv_standard reader (default: ',').",
    )
    p_ing.add_argument(
        "--decimal-comma",
        action="store_true",
        help="Parse decimal commas in csv_standard reader.",
    )
    p_ing.add_argument(
        "--timezone",
        default=None,
        help="Local timezone for naive timestamps (default: Europe/Rome).",
    )

    # readers
    sub.add_parser("readers", help="List available input readers.")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    cfg = EmToolsConfig.from_env(args.data_dir)

    if args.command == "init":
        cfg.ensure_directories()
        repo = MasterDataRepository(cfg.db_path, backups_dir=cfg.backups_dir)
        db_file = repo.init()
        print(f"Initialized workspace at '{cfg.data_dir}' and SQLite DB at '{db_file}'.")
        return 0

    if args.command == "readers":
        for reader in DEFAULT_REGISTRY.list_readers():
            print(f"  - {reader.name}: {reader.description}")
        return 0

    if args.command == "export-editor":
        cfg.ensure_directories()
        repo = MasterDataRepository(cfg.db_path, backups_dir=cfg.backups_dir)
        if not cfg.db_path.exists():
            repo.init()
        out_path = args.output or cfg.default_editor_path
        exported = export_to_editor(cfg.db_path, out_path)
        print(f"Exported master data to '{exported}'.")
        return 0

    if args.command == "import-editor":
        try:
            report = import_from_editor(
                cfg.db_path,
                args.input_xlsx,
                dry_run=args.dry_run,
                backups_dir=cfg.backups_dir,
            )
        except RoundtripValidationError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(report.format_summary())
        return 0

    if args.command == "ingest":
        tz = args.timezone or cfg.default_timezone
        kwargs: dict[str, Any] = {
            "default_timezone": tz,
            "separator": args.separator,
            "decimal_comma": args.decimal_comma,
        }
        if args.energy_carrier is not None:
            kwargs["energy_carrier"] = args.energy_carrier
        if args.channel is not None:
            kwargs["channel"] = args.channel
        if args.unit is not None:
            kwargs["unit"] = args.unit
        if args.metering_point_id is not None:
            kwargs["metering_point_id"] = args.metering_point_id
        if args.object_id is not None:
            kwargs["object_id"] = args.object_id

        target = args.input_path
        if target.is_dir():
            batch = ingest_directory(
                target, config=cfg, recursive=args.recursive, **kwargs
            )
            print(batch.format_summary())
            return 0

        try:
            res = ingest_file(target, config=cfg, reader_name=args.reader, **kwargs)
        except (LookupError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 2

        print(
            f"Ingested {res.rows_input} row(s) from '{res.file_path.name}' "
            f"(reader={res.reader_name}, mp={list(res.metering_point_ids)}, "
            f"channel={list(res.channels)}) into {res.partitions_written} partition(s) "
            f"({res.rows_stored} total deduplicated rows stored)."
        )
        for p in res.partition_files:
            print(f"  -> {p}")
        return 0

    parser.error(f"Unknown command: {args.command}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
