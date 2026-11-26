"""Packaged dbt-duckdb analytics runner for LocalEMS workspaces."""

from __future__ import annotations

import importlib.resources
import importlib.util
import json
import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from localems.config import LocalEmsConfig

_COMMANDS_WITHOUT_VARS = frozenset({"clean", "debug", "deps"})


@dataclass(frozen=True)
class DbtRunResult:
    """Result of executing a packaged dbt command against a LocalEMS workspace."""

    command: str
    returncode: int
    stdout: str
    stderr: str
    duckdb_path: Path

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def get_dbt_project_dir() -> Path:
    """Return the filesystem path to the bundled `localems` dbt project directory."""
    resource = importlib.resources.files("localems").joinpath("dbt_project")
    dbt_dir = Path(str(resource))
    if not (dbt_dir / "dbt_project.yml").is_file():
        raise FileNotFoundError(
            f"Bundled dbt project not found at '{dbt_dir}'. "
            "Reinstall localems to restore package data."
        )
    return dbt_dir


def build_dbt_env(
    config: LocalEmsConfig,
    *,
    extra_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build environment variables wiring the bundled dbt project to `config`."""
    dbt_dir = get_dbt_project_dir()
    env = os.environ.copy()
    env.update(
        {
            "LOCALEMS_DUCKDB_PATH": config.duckdb_path.resolve().as_posix(),
            "LOCALEMS_SQLITE_PATH": config.db_path.resolve().as_posix(),
            "LOCALEMS_PARQUET_GLOB": config.parquet_glob,
            "LOCALEMS_DUCKDB_EXT_DIR": config.resolved_duckdb_extensions_dir.resolve().as_posix(),
            "LOCALEMS_TIMEZONE": config.default_timezone,
            "DBT_PROFILES_DIR": dbt_dir.resolve().as_posix(),
            "DBT_TARGET_PATH": config.dbt_target_dir.resolve().as_posix(),
            "DBT_LOG_PATH": config.dbt_logs_dir.resolve().as_posix(),
            "DBT_SEND_ANONYMOUS_USAGE_STATS": "false",
        }
    )
    if extra_env:
        env.update(extra_env)
    return env


def run_dbt(
    command: str = "run",
    *,
    config: LocalEmsConfig,
    extra_args: Sequence[str] = (),
    extra_env: Mapping[str, str] | None = None,
    capture_output: bool = False,
    check: bool = False,
) -> DbtRunResult:
    """Execute a dbt command (`run`, `test`, `build`, ...) using the bundled dbt project.

    All runtime artifacts (`localems.duckdb`, dbt `target/`, dbt `logs/`, and DuckDB
    extensions) are directed to the workspace configured by `config`, so the installed
    package directory stays completely untouched and read-only safe.
    """
    if importlib.util.find_spec("dbt.cli.main") is None:
        raise RuntimeError(
            "dbt-duckdb is not installed in this Python environment. "
            'Install analytics extras with: pip install "localems[analytics]"'
        )

    dbt_dir = get_dbt_project_dir()
    config.duckdb_path.parent.mkdir(parents=True, exist_ok=True)
    config.dbt_target_dir.mkdir(parents=True, exist_ok=True)
    config.dbt_logs_dir.mkdir(parents=True, exist_ok=True)
    config.resolved_duckdb_extensions_dir.mkdir(parents=True, exist_ok=True)

    args_list = list(extra_args)
    cmd = [
        sys.executable,
        "-c",
        "import sys; from dbt.cli.main import cli; sys.argv = ['dbt', *sys.argv[1:]]; cli()",
        command,
        "--project-dir",
        str(dbt_dir),
        "--profiles-dir",
        str(dbt_dir),
    ]

    has_vars_flag = any(a == "--vars" or a.startswith("--vars=") for a in args_list)
    if command not in _COMMANDS_WITHOUT_VARS and not has_vars_flag:
        cmd.extend(["--vars", json.dumps({"report_timezone": config.default_timezone})])

    cmd.extend(args_list)

    proc = subprocess.run(
        cmd,
        env=build_dbt_env(config, extra_env=extra_env),
        capture_output=capture_output,
        text=True,
        check=False,
    )
    result = DbtRunResult(
        command=command,
        returncode=proc.returncode,
        stdout=proc.stdout or "",
        stderr=proc.stderr or "",
        duckdb_path=config.duckdb_path.resolve(),
    )
    if check and not result.ok:
        details = "\n".join(part for part in (result.stdout, result.stderr) if part)
        raise RuntimeError(
            f"dbt {command} failed with exit code {result.returncode}"
            + (f":\n{details}" if details else "")
        )
    return result
