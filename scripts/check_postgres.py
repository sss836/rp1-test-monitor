"""Run tests against a fresh, named, loopback-only PostgreSQL container."""

import os
import secrets
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import quote

import psycopg

ROOT = Path(__file__).resolve().parents[1]


def main():
    suffix = secrets.token_hex(5)
    name = f"rp1-monitor-test-{suffix}"
    database = f"rp1_monitor_test_{suffix}"
    password = secrets.token_urlsafe(32)
    with tempfile.TemporaryDirectory(prefix="rp1-monitor-test-") as directory:
        config = Path(directory) / "postgres.env"
        descriptor = os.open(config, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            stream.write(
                f"POSTGRES_DB={database}\nPOSTGRES_USER=monitor_test\nPOSTGRES_PASSWORD={password}\n"
            )
        created = False
        try:
            subprocess.run(
                [
                    "docker",
                    "run",
                    "--detach",
                    "--rm",
                    "--name",
                    name,
                    "--env-file",
                    str(config),
                    "-p",
                    "127.0.0.1::5432",
                    "postgres:18",
                ],
                check=True,
                capture_output=True,
            )
            created = True
            address = subprocess.check_output(["docker", "port", name, "5432"], text=True).strip()
            port = int(address.rsplit(":", 1)[1])
            for attempt in range(60):
                try:
                    with psycopg.connect(
                        host="127.0.0.1",
                        port=port,
                        dbname=database,
                        user="monitor_test",
                        password=password,
                        connect_timeout=1,
                    ):
                        break
                except psycopg.OperationalError:
                    if attempt == 59:
                        raise RuntimeError("临时PostgreSQL启动超时") from None
                    time.sleep(0.5)
            environment = os.environ.copy()
            environment.pop("PYTHONPATH", None)
            environment.pop("PYTHONHOME", None)
            environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
            environment["RP1_MONITOR_TEST_DATABASE_URL"] = (
                f"postgresql+psycopg://monitor_test:{quote(password)}@127.0.0.1:{port}/{database}"
            )
            result = subprocess.run(
                [sys.executable, "-m", "pytest", "-q"], cwd=ROOT, env=environment
            )
            return result.returncode
        finally:
            if created:
                subprocess.run(["docker", "rm", "--force", name], check=False, capture_output=True)


if __name__ == "__main__":
    raise SystemExit(main())
