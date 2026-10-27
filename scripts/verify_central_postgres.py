"""Verify this task's simulated PostgreSQL environment, archive, and an isolated restore copy."""

import argparse
import hashlib
import json
import os
import secrets
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
from psycopg import sql

from rp1_monitor.central_data import archive_range, verify_archive
from rp1_monitor.cli import private_write
from rp1_monitor.storage import Store

CONTAINER = "rp1-monitor-central-postgres-1"


def fingerprint(connection):
    counts = {}
    tables = connection.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname='rp1_monitor' ORDER BY tablename"
    ).fetchall()
    for (name,) in tables:
        counts[name] = connection.execute(
            sql.SQL("SELECT count(*) FROM rp1_monitor.{}").format(sql.Identifier(name))
        ).fetchone()[0]
    digest = hashlib.sha256()
    with connection.cursor(name="original_evidence_verification") as cursor:
        cursor.execute(
            "SELECT id,payload_hash,payload FROM rp1_monitor.monitor_windows ORDER BY id"
        )
        for row_id, expected, payload in cursor:
            actual = hashlib.sha256(
                json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            if actual != expected:
                raise ValueError("原始证据哈希不一致，停止验收")
            digest.update(f"{row_id}:{expected}\n".encode())
    return {"tables": counts, "raw_evidence_digest": digest.hexdigest()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    directory = args.runtime.resolve()
    record = json.loads((directory / "environment.json").read_text())
    compose = json.loads((directory / "compose.yml").read_text())
    if not record["simulated_only"] or compose["name"] != "rp1-monitor-central":
        raise ValueError("此脚本只验收本轮独立模拟环境")
    connect = dict(
        host="127.0.0.1",
        port=record["port"],
        user="postgres",
        dbname="rp1_monitor",
        password=(directory / "credentials/pg-admin-password").read_text(),
    )
    with psycopg.connect(**connect) as connection:
        if connection.execute(
            "SELECT count(*) FROM rp1_monitor.monitor_robots WHERE source_kind<>'simulated'"
        ).fetchone()[0]:
            raise ValueError("拒绝将真实采集数据作为模拟验收输出")
    archiver = Store(
        (directory / "credentials/archiver-database-url.txt").read_text(), initialize=False
    )
    try:
        end = datetime.now(timezone.utc) - timedelta(seconds=3)
        archive = archive_range(
            archiver, "DEMO-RP1-01", end - timedelta(seconds=30), end, directory / "archives"
        )
        verified = [
            verify_archive(directory / "archives" / item["file_name"], item)
            for item in archive["items"]
        ]
    finally:
        archiver.engine.dispose()
    backup_directory = directory / "backups"
    backup_directory.mkdir(mode=0o700, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(3)
    backup = backup_directory / f"monitor-{stamp}.dump"
    with psycopg.connect(**connect) as source:
        source.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        snapshot = source.execute("SELECT pg_export_snapshot()").fetchone()[0]
        expected = fingerprint(source)
        descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            subprocess.run(
                [
                    "docker",
                    "exec",
                    "--user",
                    "postgres",
                    CONTAINER,
                    "pg_dump",
                    "-Fc",
                    "-d",
                    "rp1_monitor",
                    "--snapshot",
                    snapshot,
                ],
                stdout=output,
                check=True,
            )
            output.flush()
            os.fsync(output.fileno())
        directory_fd = os.open(backup_directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    restored = "rp1_monitor_restore_check_" + secrets.token_hex(5)
    subprocess.run(
        [
            "docker",
            "exec",
            "--user",
            "postgres",
            CONTAINER,
            "createdb",
            "--template",
            "template0",
            restored,
        ],
        check=True,
    )
    with backup.open("rb") as stream:
        subprocess.run(
            [
                "docker",
                "exec",
                "-i",
                "--user",
                "postgres",
                CONTAINER,
                "pg_restore",
                "--exit-on-error",
                "-d",
                restored,
            ],
            stdin=stream,
            check=True,
        )
    with psycopg.connect(**{**connect, "dbname": restored}) as target:
        target.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        actual = fingerprint(target)
    if actual != expected:
        raise ValueError("隔离恢复副本与一致性备份快照不一致")
    with psycopg.connect(**connect) as connection:
        plan = connection.execute("""EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) SELECT id,window_end
            FROM rp1_monitor.monitor_windows WHERE robot_id='DEMO-RP1-01'
            AND window_end>now()-interval '10 minutes' ORDER BY window_end LIMIT 1200""").fetchone()[
            0
        ]
        size = connection.execute("SELECT pg_database_size(current_database())").fetchone()[0]
        correction = connection.execute(
            "SELECT operation,count(*) FROM rp1_monitor.monitor_metric_corrections GROUP BY operation"
        ).fetchall()
    report = {
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "scope": "PostgreSQL_18_and_simulated_data_only",
        "version": record["version"],
        "image": record["image"],
        "schema_tables": record["schema_tables"],
        "bind": "127.0.0.1",
        "port": record["port"],
        "legacy_imported": False,
        "original_data_deleted": False,
        "archive_verified_windows": sum(row["windows"] for row in verified),
        "archive_source_deleted": False,
        "backup_bytes": backup.stat().st_size,
        "backup_sha256": hashlib.sha256(backup.read_bytes()).hexdigest(),
        "restore_database": restored,
        "restore_snapshot_match": True,
        "snapshot": expected,
        "human_correction_operations": dict(correction),
        "physical_database_bytes": size,
        "query_plan": plan,
        "ten_robot_endurance_run": False,
        "hardware_connected": False,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    private_write(
        backup_directory / f"monitor-{stamp}.manifest.json",
        json.dumps(
            {
                "backup": backup.name,
                "restore_database": restored,
                "snapshot": expected,
                "sha256": report["backup_sha256"],
            },
            indent=2,
        ),
    )
    print(
        json.dumps(
            {
                "archive_verified_windows": report["archive_verified_windows"],
                "restore_snapshot_match": True,
                "tables": record["schema_tables"],
                "report": str(args.output),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
