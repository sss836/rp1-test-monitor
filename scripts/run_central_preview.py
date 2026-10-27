"""Run two explicit simulators and relays against the newly provisioned PostgreSQL."""

import argparse
import json
import signal
import subprocess
import sys
import time
from pathlib import Path

from rp1_monitor.cli import private_write


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--port", type=int, default=18940)
    parser.add_argument(
        "--thermal-demo", action="store_true", help="模拟温升循环，使用显著标记的演示限值与部位映射"
    )
    args = parser.parse_args()
    directory = args.runtime.resolve()
    devices = json.loads((directory / "device-config.json").read_text())
    if len(devices) != 2 or any(row["source_kind"] != "simulated" for row in devices):
        raise ValueError("此启动器仅允许两台明确标记的模拟设备")
    children, logs = [], []
    examples = Path(__file__).resolve().parents[1] / "examples"

    def launch(name, command):
        log = (directory / f"{name}.log").open("a")
        logs.append(log)
        children.append(
            subprocess.Popen(
                [sys.executable, "-m", "rp1_monitor", *command],
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        )

    try:
        launch(
            "backend",
            [
                "server",
                "--devices",
                str(directory / "device-config.json"),
                "--database-url-file",
                str(directory / "credentials/api-database-url.txt"),
                "--no-initialize",
                "--demo-read",
                "--admin-config",
                str(directory / "management/admin.json"),
                "--storage-mounts",
                str(directory / "storage-mounts.json"),
                "--port",
                str(args.port),
                *(
                    [
                        "--policy",
                        str(examples / "thermal-policy.simulated.json"),
                        "--motor-map",
                        str(examples / "motor-map.simulated.json"),
                    ]
                    if args.thermal_demo
                    else []
                ),
            ],
        )
        for index, device in enumerate(devices):
            key_file = directory / "credentials" / f"device-{index}-token.txt"
            if not key_file.exists():
                private_write(key_file, device["token"])
            launch(
                f"agent-{index}",
                [
                    "agent",
                    "--source",
                    "simulated",
                    "--simulated-dof",
                    str(24 if index == 0 else 28),
                    "--robot-id",
                    device["robot_id"],
                    "--state-dir",
                    str(directory / f"agent-{index}"),
                    "--port",
                    str(18951 + index),
                    *(["--thermal-demo"] if args.thermal_demo else []),
                ],
            )
            launch(
                f"relay-{index}",
                [
                    "relay",
                    "--source",
                    "simulated",
                    "--robot-id",
                    device["robot_id"],
                    "--collector",
                    f"http://127.0.0.1:{18951 + index}",
                    "--backend",
                    f"http://127.0.0.1:{args.port}",
                    "--token-file",
                    str(key_file),
                    "--state-dir",
                    str(directory / f"relay-{index}"),
                    "--port",
                    str(18961 + index),
                ],
            )
        print(
            f"PostgreSQL模拟大屏：http://127.0.0.1:{args.port}/\n设备管理：http://127.0.0.1:{args.port}/admin",
            flush=True,
        )
        if args.thermal_demo:
            print(
                "温升演示：限值、关节部位与20–100°C色阶均为模拟参数，未完成实机确认。", flush=True
            )
        while True:
            if any(child.poll() is not None for child in children):
                raise RuntimeError("预览服务提前退出；请检查本次runtime日志")
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        for child in reversed(children):
            if child.poll() is None:
                child.send_signal(signal.SIGINT)
        for child in children:
            try:
                child.wait(timeout=8)
            except subprocess.TimeoutExpired:
                child.terminate()
                child.wait(timeout=5)
        for log in logs:
            log.close()


if __name__ == "__main__":
    main()
