"""Add reviewed monitoring configuration tables to this task's simulated Docker database."""

import argparse
import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

CONTAINER = "rp1-monitor-central-postgres-1"
ROOT = Path(__file__).resolve().parents[1]


def database(query):
    result = subprocess.check_output(
        [
            "docker",
            "exec",
            CONTAINER,
            "gosu",
            "postgres",
            "psql",
            "-X",
            "-At",
            "-v",
            "ON_ERROR_STOP=1",
            "-d",
            "rp1_monitor",
            "-c",
            query,
        ],
        text=True,
    )
    return json.loads(result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--apply", action="store_true", help="备份后执行005；默认仅检查")
    args = parser.parse_args()
    directory = args.runtime.resolve()
    expected = str(directory / "compose.yml")
    actual = subprocess.check_output(
        [
            "docker",
            "inspect",
            CONTAINER,
            "--format",
            '{{ index .Config.Labels "com.docker.compose.project.config_files" }}',
        ],
        text=True,
    ).strip()
    if actual != expected or not (directory / "environment.json").exists():
        raise ValueError("容器不属于本次专用模拟环境，拒绝操作")
    before = database(
        "SELECT json_build_object('real_robots',(SELECT count(*) FROM rp1_monitor.monitor_robots WHERE source_kind<>'simulated'),'windows',(SELECT count(*) FROM rp1_monitor.monitor_windows),'configuration_table',to_regclass('rp1_monitor.monitor_robot_config_versions') IS NOT NULL)"
    )
    if before["real_robots"]:
        raise ValueError("此升级脚本仅用于模拟环境，不修改包含实机登记的数据库")
    if not args.apply or before["configuration_table"]:
        print(
            json.dumps(
                {
                    "checked": True,
                    "already_applied": before["configuration_table"],
                    "applied": False,
                },
                ensure_ascii=False,
            )
        )
        return
    backup_directory = directory / "backups"
    backup_directory.mkdir(mode=0o700, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = backup_directory / f"before-005-{stamp}.dump"
    descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as target:
        subprocess.run(
            [
                "docker",
                "exec",
                CONTAINER,
                "gosu",
                "postgres",
                "pg_dump",
                "-Fc",
                "-d",
                "rp1_monitor",
            ],
            stdout=target,
            check=True,
        )
        target.flush()
        os.fsync(target.fileno())
    migration = ROOT / "deploy/postgres/005_robot_configuration.review.sql"
    with migration.open("rb") as source:
        subprocess.run(
            [
                "docker",
                "exec",
                "-i",
                CONTAINER,
                "gosu",
                "postgres",
                "psql",
                "-X",
                "-v",
                "ON_ERROR_STOP=1",
                "-d",
                "rp1_monitor",
            ],
            stdin=source,
            check=True,
            stdout=subprocess.DEVNULL,
        )
    after = database(
        "SELECT json_build_object('tables',(SELECT count(*) FROM pg_tables WHERE schemaname='rp1_monitor'),'windows',(SELECT count(*) FROM rp1_monitor.monitor_windows),'configuration_table',to_regclass('rp1_monitor.monitor_robot_config_versions') IS NOT NULL)"
    )
    if not after["configuration_table"] or after["windows"] < before["windows"]:
        raise RuntimeError("升级后校验未通过，请保留备份并检查；脚本不会删除或重建数据库")
    record_path = directory / "environment.json"
    record = json.loads(record_path.read_text())
    record["schema_tables"] = after["tables"]
    record["configuration_migration"] = {
        "applied_at": stamp,
        "sha256": hashlib.sha256(migration.read_bytes()).hexdigest(),
        "backup": backup.name,
    }
    record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {
                "applied": True,
                "schema_tables": after["tables"],
                "windows_before": before["windows"],
                "windows_after": after["windows"],
                "backup": str(backup),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
