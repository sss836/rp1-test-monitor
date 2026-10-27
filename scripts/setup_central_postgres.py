"""Explicitly provision a new, isolated central environment after design approval.

Never imports old databases, changes existing containers, or removes data on failure.
Runtime files contain credentials and must remain outside version control.
"""

import argparse
import json
import re
import secrets
import socket
import subprocess
from pathlib import Path

import psycopg
from psycopg import sql
from sqlalchemy.engine import URL

from rp1_monitor.admin import password_hash
from rp1_monitor.cli import private_write

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--port", type=int, default=55433)
    parser.add_argument("--project-name", default="rp1-monitor-central")
    parser.add_argument("--robot-id", help="独立实机环境的唯一设备编号；不复用模拟编号")
    parser.add_argument("--robot-name", default="RP1 实机")
    parser.add_argument(
        "--resume-initialization",
        action="store_true",
        help="仅恢复本脚本尚未建schema的初次容器初始化；不重建数据库",
    )
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", args.project_name):
        parser.error("Compose项目名只允许小写字母、数字、下划线和连字符")
    if args.robot_id:
        from pydantic import TypeAdapter

        from rp1_monitor.models import RobotId

        TypeAdapter(RobotId).validate_python(args.robot_id)
        if args.robot_id.startswith("DEMO-"):
            parser.error("实机不能复用DEMO设备编号")
    directory = args.runtime.resolve()
    if directory.exists() and not args.resume_initialization:
        parser.error("创建目标必须是新的专用目录；不会覆盖或重建已有数据库")
    if args.resume_initialization and (
        not (directory / "compose.yml").exists() or (directory / "environment.json").exists()
    ):
        parser.error("只能恢复本脚本未完成的首次初始化")
    if not 1024 <= args.port <= 65535:
        parser.error("端口必须在1024～65535之间")
    if not args.resume_initialization:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", args.port))
    configurations = subprocess.check_output(
        [
            "docker",
            "ps",
            "-a",
            "--filter",
            f"label=com.docker.compose.project={args.project_name}",
            "--format",
            '{{.Label "com.docker.compose.project.config_files"}}',
        ],
        text=True,
    ).splitlines()
    if configurations and (
        not args.resume_initialization
        or any(value != str(directory / "compose.yml") for value in configurations)
    ):
        parser.error("同名Compose项目已存在且不属于此次恢复目录；拒绝修改")
    digests = json.loads(
        subprocess.check_output(
            ["docker", "image", "inspect", "postgres:18", "--format", "{{json .RepoDigests}}"],
            text=True,
        )
    )
    image = next((value for value in digests if value.startswith("postgres@sha256:")), None)
    if image is None:
        raise RuntimeError("请先获取官方postgres:18镜像并确认digest")
    data = directory / "data"
    archives = directory / "archives"
    credentials = directory / "credentials"
    admin_directory = directory / "management"
    passwords = {
        key: secrets.token_urlsafe(32)
        for key in ("postgres", "api", "operator", "reader", "archiver")
    }
    if args.resume_initialization:
        passwords["postgres"] = (credentials / "pg-admin-password").read_text()
    compose = {
        "name": args.project_name,
        "services": {
            "postgres": {
                "image": image,
                "restart": "unless-stopped",
                "environment": {
                    "POSTGRES_DB": "rp1_monitor",
                    "POSTGRES_USER": "postgres",
                    "POSTGRES_PASSWORD_FILE": "/run/secrets/pg_admin_password",
                    "POSTGRES_INITDB_ARGS": "--auth-host=scram-sha-256 --auth-local=peer",
                },
                "secrets": ["pg_admin_password"],
                "ports": [f"127.0.0.1:{args.port}:5432"],
                "volumes": [f"{data}:/var/lib/postgresql"],
                "shm_size": "256m",
                "mem_limit": "2g",
                "command": [
                    "postgres",
                    "-c",
                    "timezone=UTC",
                    "-c",
                    "password_encryption=scram-sha-256",
                    "-c",
                    "statement_timeout=30s",
                    "-c",
                    "idle_in_transaction_session_timeout=30s",
                    "-c",
                    "lock_timeout=5s",
                    "-c",
                    "max_connections=40",
                    "-c",
                    "shared_buffers=256MB",
                ],
                "healthcheck": {
                    "test": [
                        "CMD",
                        "gosu",
                        "postgres",
                        "pg_isready",
                        "-U",
                        "postgres",
                        "-d",
                        "rp1_monitor",
                    ],
                    "interval": "3s",
                    "timeout": "3s",
                    "retries": 20,
                },
                "networks": ["monitor_db"],
            }
        },
        "secrets": {"pg_admin_password": {"file": str(credentials / "pg-admin-password")}},
        "networks": {
            "monitor_db": {
                "driver": "bridge",
                "driver_opts": {"com.docker.network.bridge.enable_ip_masquerade": "false"},
            }
        },
    }
    # JSON is a YAML subset accepted by Compose; arguments contain no secret values.
    if args.resume_initialization:
        existing = json.loads((directory / "compose.yml").read_text())
        legacy_compose = json.loads(json.dumps(compose))
        legacy_compose["services"]["postgres"]["healthcheck"]["test"] = [
            "CMD-SHELL",
            "pg_isready -U postgres -d rp1_monitor",
        ]
        if existing not in (compose, legacy_compose):
            parser.error("恢复目标与本脚本的专用容器、目录或端口配置不符")
    # All collision and resume identity checks precede even directory permission changes.
    directory.mkdir(parents=True, mode=0o700, exist_ok=args.resume_initialization)
    # PG18's PGDATA is private 0700 below this traversable parent; host runtime stays 0700.
    data.mkdir(mode=0o711, exist_ok=args.resume_initialization)
    data.chmod(0o711)
    for path in (archives, credentials, admin_directory):
        path.mkdir(mode=0o700, exist_ok=args.resume_initialization)
    if args.resume_initialization:
        (directory / "compose.yml").write_text(json.dumps(compose, indent=2))
    else:
        private_write(credentials / "pg-admin-password", passwords["postgres"])
        private_write(directory / "compose.yml", json.dumps(compose, indent=2))
    subprocess.run(
        ["docker", "compose", "-f", str(directory / "compose.yml"), "up", "-d", "--wait"],
        check=True,
    )
    with psycopg.connect(
        host="127.0.0.1",
        port=args.port,
        dbname="rp1_monitor",
        user="postgres",
        password=passwords["postgres"],
        autocommit=True,
    ) as connection:
        if connection.execute("SELECT 1 FROM pg_namespace WHERE nspname='rp1_monitor'").fetchone():
            raise RuntimeError("目标schema已存在；不会自动重放迁移或覆盖已有数据库")
        for filename in (
            "001_schema.review.sql",
            "002_permissions.review.sql",
            "004_admin.review.sql",
            "005_robot_configuration.review.sql",
        ):
            connection.execute((ROOT / "deploy/postgres" / filename).read_text(), prepare=False)
        identities = [
            ("rp1_central_api", "api", "rp1_monitor_app"),
            ("rp1_central_operator", "operator", "rp1_monitor_admin"),
            ("rp1_central_reader", "reader", "rp1_monitor_reader"),
            ("rp1_central_archiver", "archiver", "rp1_monitor_archiver"),
        ]
        for login, key, group in identities:
            connection.execute(
                sql.SQL(
                    "CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS CONNECTION LIMIT 10 PASSWORD {}"
                ).format(sql.Identifier(login), sql.Literal(passwords[key]))
            )
            connection.execute(
                sql.SQL("GRANT {} TO {}").format(sql.Identifier(group), sql.Identifier(login))
            )
            connection.execute(
                sql.SQL("ALTER ROLE {} SET search_path=rp1_monitor,pg_catalog").format(
                    sql.Identifier(login)
                )
            )
            connection.execute(
                sql.SQL("ALTER ROLE {} SET statement_timeout='15s'").format(sql.Identifier(login))
            )
            connection.execute(
                sql.SQL("ALTER ROLE {} SET idle_in_transaction_session_timeout='30s'").format(
                    sql.Identifier(login)
                )
            )
            url = URL.create(
                "postgresql+psycopg",
                username=login,
                password=passwords[key],
                host="127.0.0.1",
                port=args.port,
                database="rp1_monitor",
            )
            private_write(
                credentials / f"{key}-database-url.txt", url.render_as_string(hide_password=False)
            )
        version = connection.execute("SELECT version()").fetchone()[0]
        tables = connection.execute(
            "SELECT count(*) FROM pg_tables WHERE schemaname='rp1_monitor'"
        ).fetchone()[0]
    password = secrets.token_urlsafe(20)
    admin_config = {
        "users": [
            {
                "username": "admin",
                "password_hash": password_hash(password),
                "database_url": (credentials / "operator-database-url.txt").read_text(),
            }
        ],
        "device_key_directory": str(admin_directory / "device-keys"),
    }
    private_write(
        admin_directory / "admin.json", json.dumps(admin_config, ensure_ascii=False, indent=2)
    )
    private_write(admin_directory / "initial-login.txt", f"用户名：admin\n初始密码：{password}\n")
    devices = [
        dict(
            robot_id=f"DEMO-RP1-{index + 1:02}",
            name=f"模拟 RP1 {index + 1:02}",
            source_kind="simulated",
            token=secrets.token_urlsafe(32),
        )
        for index in range(2)
    ]
    if args.robot_id:
        devices = [
            dict(
                robot_id=args.robot_id,
                name=args.robot_name,
                source_kind="ros2",
                token=secrets.token_urlsafe(32),
            )
        ]
    private_write(
        directory / "device-config.json", json.dumps(devices, ensure_ascii=False, indent=2)
    )
    private_write(credentials / "viewer-token.txt", secrets.token_urlsafe(32))
    private_write(
        directory / "storage-mounts.json",
        json.dumps(
            {
                "database_authority": f"127.0.0.1:{args.port}/rp1_monitor",
                "mounts": {
                    name: {
                        "path": str(path),
                        "device_id": path.stat().st_dev,
                        "reserve_bytes": 10 * 1024**3,
                    }
                    for name, path in (("database", data), ("archives", archives))
                },
            },
            indent=2,
        ),
    )
    record = {
        "created": True,
        "image": image,
        "version": version,
        "schema_tables": tables,
        "database": "rp1_monitor",
        "port": args.port,
        "bind": "127.0.0.1",
        "simulated_only": not bool(args.robot_id),
        "project_name": args.project_name,
        "legacy_imported": False,
        "source_deleted": False,
        "postgres_data_directory": str(data),
        "archive_directory": str(archives),
    }
    private_write(directory / "environment.json", json.dumps(record, ensure_ascii=False, indent=2))
    print(
        json.dumps(
            {
                "created": True,
                "port": args.port,
                "tables": tables,
                "runtime": str(directory),
                "admin_login_file": str(admin_directory / "initial-login.txt"),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
