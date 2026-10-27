"""Real local processes, native diagnostics, durable export, and offline recovery."""

import argparse
import hashlib
import io
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

import httpx


def until(check, timeout=25):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            result = check()
            if result:
                return result
        except (httpx.HTTPError, KeyError, ValueError, IndexError):
            pass
        time.sleep(0.15)
    raise AssertionError("Local operation check timed out")


def stop(child):
    if child.poll() is None:
        child.send_signal(signal.SIGINT)
        try:
            child.wait(timeout=8)
        except subprocess.TimeoutExpired:
            child.terminate()
            child.wait(timeout=5)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output", type=Path, default=Path("docs/operations-e2e-verification.json")
    )
    parser.add_argument(
        "--screenshot", type=Path, default=Path("docs/diagnostics-desktop-preview.png")
    )
    args = parser.parse_args()
    args.screenshot = args.screenshot.resolve()
    args.screenshot.parent.mkdir(parents=True, exist_ok=True)
    handles = []
    for _ in range(3):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        handles.append(sock)
    server_port, collector_port, agent_port = [s.getsockname()[1] for s in handles]
    for handle in handles:
        handle.close()
    backend = f"http://127.0.0.1:{server_port}"
    agent = f"http://127.0.0.1:{agent_port}"
    children = []
    with tempfile.TemporaryDirectory(prefix="rp1-operations-e2e-") as temp:
        root = Path(temp)
        config = root / "devices.json"
        token = root / "device-token.txt"
        secret = secrets.token_urlsafe(32)
        token.write_text(secret)
        token.chmod(0o600)
        config.write_text(
            json.dumps(
                [
                    {
                        "robot_id": "OPS-SIM-01",
                        "name": "诊断模拟设备",
                        "source_kind": "simulated",
                        "token": secret,
                    }
                ]
            )
        )
        config.chmod(0o600)
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        env.pop("PYTHONHOME", None)
        for key in list(env):
            if key.startswith("RP1_AI_") or key in {
                "DEEPSEEK_API_KEY",
                "DASHSCOPE_API_KEY",
                "DASHSCOPE_INTL_API_KEY",
                "OPENAI_API_KEY",
            }:
                env.pop(key)

        def spawn(command, label):
            log = (root / (label + ".log")).open("a")
            child = subprocess.Popen(
                [sys.executable, "-m", "rp1_monitor", *command],
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            children.append((child, log))
            return child

        server_command = [
            "server",
            "--devices",
            str(config),
            "--database-url",
            f"sqlite:///{root / 'central.sqlite'}",
            "--port",
            str(server_port),
            "--demo-read",
        ]
        try:
            server = spawn(server_command, "server")
            spawn(
                [
                    "agent",
                    "--source",
                    "simulated",
                    "--robot-id",
                    "OPS-SIM-01",
                    "--state-dir",
                    str(root / "agent"),
                    "--port",
                    str(collector_port),
                ],
                "collector",
            )
            spawn(
                [
                    "relay",
                    "--source",
                    "simulated",
                    "--robot-id",
                    "OPS-SIM-01",
                    "--collector",
                    f"http://127.0.0.1:{collector_port}",
                    "--backend",
                    backend,
                    "--token-file",
                    str(token),
                    "--state-dir",
                    str(root / "relay"),
                    "--port",
                    str(agent_port),
                ],
                "upper-computer",
            )
            with httpx.Client(timeout=5, trust_env=False) as client:

                def snapshots():
                    response = client.get(backend + "/api/v1/robot-monitors")
                    response.raise_for_status()
                    return response.json()["items"]

                until(lambda: snapshots()[0]["health"]["motors"]["available"] == 24)
                local = client.get(agent + "/api/v1/doctor")
                local.raise_for_status()
                assert local.json()["validation_scope"] == "simulated"
                central = backend + "/api/v1/robot-monitors/OPS-SIM-01"
                caps = client.get(central + "/capabilities")
                caps.raise_for_status()
                saved_response = client.post(
                    central + "/diagnostics", json={"metrics": ["joint.0.motor_temperature"]}
                )
                saved_response.raise_for_status()
                saved = saved_response.json()
                report_id = saved["report_id"]
                response = client.get(central + f"/diagnostics/{report_id}/bundle")
                response.raise_for_status()
                with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                    for line in archive.read("SHA256SUMS").decode().splitlines():
                        digest, name = line.split("  ")
                        assert hashlib.sha256(archive.read(name)).hexdigest() == digest
                rules = client.post(
                    central + "/assistant",
                    json={
                        "question": "解释温度数据来源",
                        "metrics": ["joint.0.motor_temperature"],
                        "use_model": False,
                    },
                )
                rules.raise_for_status()
                assert rules.json()["mode"] == "local_rules"
                # Exercise the actual native shell, local diagnostic HTTP call and screenshot.
                qt_code = """
import sys
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer
from rp1_monitor.app_shell import AppMonitorWindow
app=QApplication([]);window=AppMonitorWindow(sys.argv[1]);window.show();window.operations_panel.server.setText(sys.argv[3]);window.connect_source()
def models():
    window.navigate(5)
    window.operations_panel.start('models')
def probe():
    panel=window.operations_panel
    assert 'deepseek' in panel.models.items
    panel.models.provider.setCurrentIndex(panel.models.provider.findData('deepseek'))
    panel.start('probe')
def query():
    assert 'API Key' in window.operations_panel.models.status.text()
    window.navigate(5)
    window.operations_panel.question.setFocus()
    window.operations_panel.question.setPlainText('为什么这台机器的健康监测不完整？')
    window.operations_panel.start('ask')
def capture():
    assert window.operations_panel.table.rowCount()==6
    assert 'local_rules' in window.operations_panel.output.toPlainText()
    assert len(window.operations_panel.history)==2
    assert not window.operations_panel.busy
    assert window.grab().save(sys.argv[2])
    window.close();app.quit()
QTimer.singleShot(1500,models);QTimer.singleShot(2200,probe);QTimer.singleShot(2900,query);QTimer.singleShot(4300,capture);app.exec()
"""
                qt_env = {**env, "QT_QPA_PLATFORM": "offscreen"}
                subprocess.run(
                    [sys.executable, "-c", qt_code, agent, str(args.screenshot), backend],
                    env=qt_env,
                    check=True,
                    timeout=20,
                    capture_output=True,
                )
                before = client.get(agent + "/api/v1/state").json()["sampled_at"]
                until(lambda: client.get(agent + "/api/v1/state").json()["sampled_at"] > before)
                stop(server)
                pending = until(
                    lambda: (
                        client.get(agent + "/api/v1/state").json()["upload"]["pending_windows"] >= 2
                    )
                )
                server = spawn(server_command, "restarted-server")
                until(lambda: snapshots()[0]["online"])
                until(
                    lambda: (
                        client.get(agent + "/api/v1/state").json()["upload"]["pending_windows"] <= 1
                    )
                )
                persisted = client.get(central + f"/diagnostics/{report_id}")
                persisted.raise_for_status()
                assert persisted.json()["sha256"] == saved["sha256"]
                result = {
                    "scope": "local_simulated_processes_not_hardware",
                    "topology": "robot_collector_to_upper_computer_relay_to_central",
                    "native_diagnostics_ui": True,
                    "native_conversation_http": True,
                    "native_model_catalog_http": True,
                    "native_model_configuration_error": True,
                    "doctor": True,
                    "capabilities": len(caps.json()["items"]),
                    "bundle_hashes_verified": True,
                    "local_rules_mode_explicit": True,
                    "collector_continues_after_app_exit": True,
                    "backend_offline_cache_observed": bool(pending),
                    "cache_recovered": True,
                    "diagnostic_record_survives_restart": True,
                }
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
                print(json.dumps(result, ensure_ascii=False))
        finally:
            for child, log in reversed(children):
                stop(child)
                log.close()


if __name__ == "__main__":
    main()
