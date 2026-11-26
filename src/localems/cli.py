"""Command-line interface for LocalEMS (init, ingest, export-editor, import-editor)."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import polars as pl

from localems.analytics import run_dbt
from localems.config import LocalEmsConfig
from localems.db.repository import MasterDataRepository
from localems.ingest import ingest_directory, ingest_file
from localems.readers.registry import DEFAULT_REGISTRY
from localems.reporting import EnergyReporter, export_dataframe
from localems.roundtrip import RoundtripValidationError, export_to_editor, import_from_editor


def _split_multi_str(values: list[str] | None) -> list[str] | None:
    if not values:
        return None
    out: list[str] = []
    for item in values:
        for part in item.split(","):
            cleaned = part.strip()
            if cleaned:
                out.append(cleaned)
    return out or None


def _split_multi_int(values: list[str] | None) -> list[int] | None:
    parts = _split_multi_str(values)
    return [int(p) for p in parts] if parts else None


def _print_or_export(
    df: pl.DataFrame,
    output_path: Path | None,
    *,
    sheet_name: str = "report",
    limit: int | None = None,
) -> None:
    if output_path is not None:
        saved = export_dataframe(df, output_path, sheet_name=sheet_name)
        print(f"Exported {df.height} row(s) to '{saved}'.")
        return

    display_df = df.head(limit) if (limit is not None and df.height > limit) else df
    with pl.Config(tbl_rows=max(display_df.height, 20), tbl_cols=25, tbl_width_chars=160):
        print(display_df)
    if limit is not None and df.height > limit:
        print(f"... showing first {limit} of {df.height} row(s). Use -o <file.csv|.xlsx> for all.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="localems",
        description=(
            "LocalEMS: 15-minute utility ingestion (Parquet), SQLite master-data roundtrip "
            "(.xlsx), and quarterly energy reporting toolkit."
        ),
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="Workspace data directory (default: LOCALEMS_DATA_DIR or ./data).",
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
    p_ing.add_argument(
        "--no-archive",
        action="store_true",
        help="Keep imported files in place instead of moving them to <data-dir>/archive/.",
    )
    p_ing.add_argument(
        "--archive-dir",
        type=Path,
        default=None,
        help="Custom archive directory for imported files (default: <data-dir>/archive).",
    )
    p_ing.add_argument(
        "--force",
        action="store_true",
        help="Re-import files even if an identical SHA-256 hash was already imported.",
    )

    # readers
    sub.add_parser("readers", help="List available input readers.")

    # dbt (run packaged dbt-duckdb models & DQ tests against the configured workspace)
    p_dbt = sub.add_parser(
        "dbt",
        help="Run bundled dbt-duckdb commands (run, test, build, ...) for this workspace.",
    )
    p_dbt.add_argument(
        "dbt_command",
        help="dbt subcommand to execute (e.g. run, test, build, compile, ls, debug).",
    )
    p_dbt.add_argument(
        "dbt_args",
        nargs=argparse.REMAINDER,
        help="Additional arguments forwarded directly to dbt (e.g. --select ...).",
    )

    # report (standardized queries & CSV/XLSX exports over marts, SQLite & Parquet)
    p_rep = sub.add_parser(
        "report",
        help="Query consumption, PV KPIs, plausibility, groups, or raw 15-min intervals.",
    )
    rep_sub = p_rep.add_subparsers(dest="report_kind", required=True)

    # report consumption
    p_rc = rep_sub.add_parser(
        "consumption",
        help="Query allocated energy consumption by period, group, sub_group, or object.",
    )
    p_rc.add_argument(
        "--period",
        choices=("day", "month", "quarter", "year", "total"),
        default="month",
        help="Time rollup granularity (default: month).",
    )
    p_rc.add_argument(
        "--group-by",
        action="append",
        default=None,
        help="Grouping level(s): object, group, sub_group, allocation_type, carrier (repeatable).",
    )
    p_rc.add_argument("--carrier", action="append", default=None, help="Filter energy_carrier.")
    p_rc.add_argument("--group", action="append", default=None, help="Filter object group.")
    p_rc.add_argument("--sub-group", action="append", default=None, help="Filter sub_group.")
    p_rc.add_argument("--object", action="append", default=None, help="Filter object_id.")
    p_rc.add_argument(
        "--allocation-type", action="append", default=None, help="Filter allocation_type."
    )
    p_rc.add_argument("--year", action="append", default=None, help="Filter report year(s).")
    p_rc.add_argument("--quarter", action="append", default=None, help="Filter quarter(s) 1-4.")
    p_rc.add_argument("--month", action="append", default=None, help="Filter month(s) 1-12.")
    p_rc.add_argument("--from", dest="date_from", default=None, help="Start date YYYY-MM-DD.")
    p_rc.add_argument("--to", dest="date_to", default=None, help="End date YYYY-MM-DD.")
    p_rc.add_argument(
        "--pivot",
        action="store_true",
        help="Pivot time periods into columns (useful for Excel tables).",
    )
    p_rc.add_argument("-o", "--output", type=Path, default=None, help="Export to .csv or .xlsx.")

    # report pv
    p_rpv = rep_sub.add_parser(
        "pv",
        help="Query PV production, feed-in, self-consumption, and autarky KPIs.",
    )
    p_rpv.add_argument(
        "--period",
        choices=("day", "month", "quarter", "year", "total"),
        default="month",
        help="Time rollup granularity (default: month).",
    )
    p_rpv.add_argument(
        "--group-by",
        action="append",
        default=None,
        help="Grouping level(s): site, group, sub_group, object, plant_mode (repeatable).",
    )
    p_rpv.add_argument("--site", action="append", default=None, help="Filter site meter ID.")
    p_rpv.add_argument("--plant-mode", action="append", default=None, help="Filter plant_mode.")
    p_rpv.add_argument("--group", action="append", default=None, help="Filter object group.")
    p_rpv.add_argument("--sub-group", action="append", default=None, help="Filter sub_group.")
    p_rpv.add_argument("--object", action="append", default=None, help="Filter object_id.")
    p_rpv.add_argument("--year", action="append", default=None, help="Filter report year(s).")
    p_rpv.add_argument("--quarter", action="append", default=None, help="Filter quarter(s) 1-4.")
    p_rpv.add_argument("--month", action="append", default=None, help="Filter month(s) 1-12.")
    p_rpv.add_argument("--from", dest="date_from", default=None, help="Start date YYYY-MM-DD.")
    p_rpv.add_argument("--to", dest="date_to", default=None, help="End date YYYY-MM-DD.")
    p_rpv.add_argument("-o", "--output", type=Path, default=None, help="Export to .csv or .xlsx.")

    # report plausibility
    p_rpl = rep_sub.add_parser(
        "plausibility",
        help="Query monthly completeness, meter-tree, and residual plausibility checks.",
    )
    p_rpl.add_argument("--carrier", action="append", default=None, help="Filter energy_carrier.")
    p_rpl.add_argument("--group", action="append", default=None, help="Filter object group.")
    p_rpl.add_argument("--sub-group", action="append", default=None, help="Filter sub_group.")
    p_rpl.add_argument("--object", action="append", default=None, help="Filter object_id.")
    p_rpl.add_argument("--meter", action="append", default=None, help="Filter metering_point_id.")
    p_rpl.add_argument("--year", action="append", default=None, help="Filter report year(s).")
    p_rpl.add_argument("--quarter", action="append", default=None, help="Filter quarter(s) 1-4.")
    p_rpl.add_argument("--month", action="append", default=None, help="Filter month(s) 1-12.")
    p_rpl.add_argument("-o", "--output", type=Path, default=None, help="Export to .csv or .xlsx.")

    # report intervals (raw 15-min Parquet data for 1 or more meters)
    p_ri = rep_sub.add_parser(
        "intervals",
        help="Query raw 15-minute measurements directly from Parquet for specific meter(s).",
    )
    p_ri.add_argument(
        "--meter",
        action="append",
        default=None,
        help="Metering point ID(s) to query (repeatable or comma-separated).",
    )
    p_ri.add_argument("--carrier", default=None, help="Filter energy_carrier.")
    p_ri.add_argument("--channel", action="append", default=None, help="Filter channel(s).")
    p_ri.add_argument("--year", type=int, default=None, help="Filter Parquet partition year.")
    p_ri.add_argument("--from", dest="start", default=None, help="Inclusive start date/datetime.")
    p_ri.add_argument("--to", dest="end", default=None, help="Exclusive end date/datetime.")
    p_ri.add_argument(
        "--utc-only", action="store_true", help="Omit converted timestamp_local column."
    )
    p_ri.add_argument(
        "--limit", type=int, default=40, help="Max rows printed to console (default: 40)."
    )
    p_ri.add_argument("-o", "--output", type=Path, default=None, help="Export all rows to file.")

    # report groups
    p_rg = rep_sub.add_parser(
        "groups",
        help="List all configured object groups and sub_groups from SQLite.",
    )
    p_rg.add_argument("-o", "--output", type=Path, default=None, help="Export to .csv or .xlsx.")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    cfg = LocalEmsConfig.from_env(args.data_dir)

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
        archive_enabled = not args.no_archive
        if target.is_dir():
            batch = ingest_directory(
                target,
                config=cfg,
                recursive=args.recursive,
                archive=archive_enabled,
                archive_dir=args.archive_dir,
                force=args.force,
                **kwargs,
            )
            print(batch.format_summary())
            return 0

        try:
            res = ingest_file(
                target,
                config=cfg,
                reader_name=args.reader,
                archive=archive_enabled,
                archive_dir=args.archive_dir,
                force=args.force,
                **kwargs,
            )
        except (LookupError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 2

        print(res.format_summary())
        return 0

    if args.command == "dbt":
        try:
            res = run_dbt(
                args.dbt_command,
                config=cfg,
                extra_args=args.dbt_args,
                capture_output=False,
            )
        except (RuntimeError, FileNotFoundError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
        return res.returncode

    if args.command == "report":
        reporter = EnergyReporter(cfg)
        try:
            if args.report_kind == "consumption":
                dims = _split_multi_str(args.group_by)
                df = reporter.consumption(
                    period=args.period,
                    group_by=dims if dims is not None else ("object",),
                    energy_carrier=_split_multi_str(args.carrier),
                    group=_split_multi_str(args.group),
                    sub_group=_split_multi_str(args.sub_group),
                    object_id=_split_multi_str(args.object),
                    allocation_type=_split_multi_str(args.allocation_type),
                    year=_split_multi_int(args.year),
                    quarter=_split_multi_int(args.quarter),
                    month=_split_multi_int(args.month),
                    date_from=args.date_from,
                    date_to=args.date_to,
                    pivot=args.pivot,
                )
                _print_or_export(df, args.output, sheet_name="consumption")
                return 0

            if args.report_kind == "pv":
                dims = _split_multi_str(args.group_by)
                df = reporter.pv_summary(
                    period=args.period,
                    group_by=dims if dims is not None else ("site",),
                    site_metering_point_id=_split_multi_str(args.site),
                    plant_mode=_split_multi_str(args.plant_mode),
                    group=_split_multi_str(args.group),
                    sub_group=_split_multi_str(args.sub_group),
                    object_id=_split_multi_str(args.object),
                    year=_split_multi_int(args.year),
                    quarter=_split_multi_int(args.quarter),
                    month=_split_multi_int(args.month),
                    date_from=args.date_from,
                    date_to=args.date_to,
                )
                _print_or_export(df, args.output, sheet_name="pv_summary")
                return 0

            if args.report_kind == "plausibility":
                df = reporter.plausibility(
                    energy_carrier=_split_multi_str(args.carrier),
                    group=_split_multi_str(args.group),
                    sub_group=_split_multi_str(args.sub_group),
                    object_id=_split_multi_str(args.object),
                    metering_point_id=_split_multi_str(args.meter),
                    year=_split_multi_int(args.year),
                    quarter=_split_multi_int(args.quarter),
                    month=_split_multi_int(args.month),
                )
                _print_or_export(df, args.output, sheet_name="plausibility")
                return 0

            if args.report_kind == "intervals":
                df = reporter.intervals(
                    metering_point_ids=_split_multi_str(args.meter),
                    energy_carrier=args.carrier,
                    channel=_split_multi_str(args.channel),
                    year=args.year,
                    start=args.start,
                    end=args.end,
                    local_time=not args.utc_only,
                )
                _print_or_export(df, args.output, sheet_name="intervals", limit=args.limit)
                return 0

            if args.report_kind == "groups":
                df = reporter.groups()
                _print_or_export(df, args.output, sheet_name="groups")
                return 0
        except (FileNotFoundError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 2

    parser.error(f"Unknown command: {args.command}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
