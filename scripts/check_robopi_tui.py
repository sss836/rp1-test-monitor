"""Run three local simulated processes; never SSH, install packages or open CAN."""

import argparse
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]


def port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def until(check):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            result = check()
            if result:
                return result
        except (httpx.HTTPError, ValueError, KeyError):
            pass
        time.sleep(0.1)
    raise RuntimeError("本机模拟验证超时")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "docs/robopi-tui-verification.json")
    parser.add_argument("--screenshot", type=Path)
    parser.add_argument("--preview-seconds", type=int, default=0)
    args = parser.parse_args()
    children, logs = [], []
    with tempfile.TemporaryDirectory(prefix="rp1-tui-software-") as directory:
        work = Path(directory)
        identity, token = "TUI-SOFTWARE-REPLAY", secrets.token_urlsafe(32)
        for filename, content in (
            (
                "devices.json",
                json.dumps(
                    [
                        {
                            "robot_id": identity,
                            "name": "TUI 软件回放 · 模拟",
                            "source_kind": "simulated",
                            "token": token,
                        }
                    ]
                ),
            ),
            ("token", token),
        ):
            fd = os.open(work / filename, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "w") as stream:
                stream.write(content)
        fixture = work / "synthetic_executor.py"
        fixture.write_text("""import json, sys, uuid
from datetime import datetime, timezone
from rp1_monitor.robopi_tui import PASSIVE_PLUGINS
deps={'RP013':['RP012'],'RP021':['RP020'],'RP022':['RP021']}
plugins={key:{'id':key,'version':'1.0','kind':'test','safety':'passive','lifecycle':'testing','clients':['tui','csharp'],'depends_on':deps.get(key,[]),'documentation':{'title':label+' · 合成回放','criterion':'仅验证软件展示，不接触机器人或总线'}} for key,label in PASSIVE_PLUGINS.items()}
if '--list' in sys.argv:
 print(json.dumps({'schema_version':3,'tests':list(plugins.values())}))
else:
 selected=sys.argv[sys.argv.index('--run')+1]
 order=[]
 def visit(key):
  for dep in deps.get(key,[]):visit(dep)
  order.append(key)
 visit(selected)
 t=datetime.now(timezone.utc).isoformat()
 rows=[{'id':key,'result':'needs_review' if key==selected else 'pass','status':'needs_review' if key==selected else 'pass','plugin':plugins[key],'message':'合成回放，仅验证软件调用、依赖与展示；没有读取真实硬件','details':{'simulated':True}} for key in order]
 print(json.dumps({'schema_version':3,'run_id':str(uuid.uuid4()),'tui_version':'1.0.0','client':'csharp','selected':selected,'started_at':t,'finished_at':t,'outcome':'incomplete','summary':{'pass':len(rows)-1,'fail':0,'needs_review':1,'skip':0,'error':0},'results':rows}))
""")
        central_port, agent_port, relay_port = port(), port(), port()
        central, agent, relay = [
            f"http://127.0.0.1:{n}" for n in (central_port, agent_port, relay_port)
        ]

        def launch(name, arguments):
            log = (work / f"{name}.log").open("w")
            logs.append(log)
            child = subprocess.Popen(
                [sys.executable, "-m", "rp1_monitor", *map(str, arguments)],
                cwd=ROOT,
                stdout=log,
                stderr=log,
            )
            children.append(child)

        try:
            launch(
                "central",
                [
                    "server",
                    "--devices",
                    work / "devices.json",
                    "--database-url",
                    f"sqlite:///{work / 'central.sqlite'}",
                    "--demo-read",
                    "--port",
                    central_port,
                ],
            )
            launch(
                "agent",
                [
                    "agent",
                    "--source",
                    "simulated",
                    "--robot-id",
                    identity,
                    "--state-dir",
                    work / "agent",
                    "--port",
                    agent_port,
                    "--robopi-tui-executor",
                    fixture,
                ],
            )
            launch(
                "relay",
                [
                    "relay",
                    "--source",
                    "simulated",
                    "--robot-id",
                    identity,
                    "--collector",
                    agent,
                    "--backend",
                    central,
                    "--token-file",
                    work / "token",
                    "--state-dir",
                    work / "relay",
                    "--port",
                    relay_port,
                ],
            )
            with httpx.Client(timeout=3, trust_env=False) as client:
                until(lambda: client.get(relay + "/api/v1/state").raise_for_status().json())
                status = client.get(relay + "/api/v1/board-diagnostics").raise_for_status().json()
                expected_reports = set()
                selected_checks = ("RP007", "RP008", "RP012", "RP013", "RP022", "RP029")
                for index, plugin_id in enumerate(selected_checks):
                    if index:
                        time.sleep(status["cooldown_remaining_seconds"])
                    started = client.post(
                        relay + "/api/v1/board-diagnostics/runs",
                        json={"robot_id": identity, "plugin_id": plugin_id},
                    )
                    assert started.status_code == 202
                    job_id = started.json()["job_id"]

                    def finished():
                        result = (
                            client.get(relay + "/api/v1/board-diagnostics")
                            .raise_for_status()
                            .json()
                        )
                        job = result.get("job") or {}
                        if job.get("job_id") == job_id and job.get("state") == "error":
                            raise AssertionError(job["error"])
                        return (
                            result
                            if job.get("job_id") == job_id and job.get("state") == "completed"
                            else None
                        )

                    status = until(finished)
                    assert status["latest"]["board_diagnostics"]["selected"] == plugin_id
                    expected_reports.add(status["latest"]["report_id"])

                def all_received():
                    history = (
                        client.get(central + f"/api/v1/robot-monitors/{identity}/diagnostics")
                        .raise_for_status()
                        .json()
                    )
                    return (
                        history
                        if expected_reports <= {row["report_id"] for row in history["items"]}
                        else None
                    )

                until(all_received)
                saved = until(
                    lambda: (
                        client.get(central + f"/api/v1/robot-monitors/{identity}/board-diagnostics")
                        .raise_for_status()
                        .json()
                        .get("latest")
                    )
                )
                assert saved == status["latest"] and saved["source_kind"] == "simulated"
                assert saved["board_diagnostics"]["outcome"] == "incomplete"
                central_checks = (
                    client.get(central + f"/api/v1/robot-monitors/{identity}/board-diagnostics")
                    .raise_for_status()
                    .json()["checks"]
                )
                assert len(central_checks) == 14
                checked = {item["id"] for item in central_checks if item["latest"]}
                assert checked == {*selected_checks, "RP020", "RP021"}
                assert (
                    next(item for item in central_checks if item["id"] == "RP007")["latest"][
                        "result"
                    ]
                    == "needs_review"
                )
                assert all(
                    item["latest"]["source_kind"] == "simulated"
                    for item in central_checks
                    if item["latest"]
                )
                assert client.post(
                    central + f"/api/v1/robot-monitors/{identity}/board-diagnostics/runs",
                    json={"plugin_id": "RP007"},
                ).status_code in {404, 405}
                snapshot = client.get(relay + "/api/v1/state").raise_for_status().json()
            if args.screenshot:
                args.screenshot.parent.mkdir(parents=True, exist_ok=True)
                qt_code = """import json,sys
import httpx
from PySide6.QtWidgets import QApplication,QScrollArea
from PySide6.QtCore import QTimer
from rp1_monitor.app_shell import AppMonitorWindow
app=QApplication([]);window=AppMonitorWindow(sys.argv[1]);window.resize(1440,1000);window.show()
window.on_frame(httpx.get(sys.argv[1]+'/api/v1/state').json());window.navigate(6)
panel=window.operations_panel.board_panel;panel.request('board_status')
def capture():
 assert '模拟数据' in panel.output.toPlainText()
 for area in window.operations_panel.findChildren(QScrollArea):
  if area.widget()==panel.parentWidget(): area.ensureWidgetVisible(panel)
 app.processEvents();window.grab().save(sys.argv[2]);window.close();app.quit()
QTimer.singleShot(1800,capture);QTimer.singleShot(10000,app.quit);app.exec()
"""
                subprocess.run(
                    [sys.executable, "-c", qt_code, relay, str(args.screenshot.resolve())],
                    cwd=ROOT,
                    env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
                    check=True,
                    timeout=15,
                )
                assert args.screenshot.is_file()
            evidence = {
                "date": "2026-10-26",
                "scope": "local_simulated_protocol_and_processes_not_hardware",
                "upstream_commit": "706df3d226c6e2deb3467c0eabce507f309fdc0f",
                "schema_version": 3,
                "three_process_transfer": True,
                "selected_checks_executed": list(selected_checks),
                "per_check_rows": len(central_checks),
                "per_check_history_preserved": sorted(checked),
                "reports_received": len(expected_reports),
                "native_executor_invoked": "synthetic_fixture_only",
                "simulation_marker": saved["validation_scope"],
                "needs_review_preserved": True,
                "central_execution_route_absent": True,
                "telemetry_still_available": bool(snapshot.get("values")),
                "qt_screenshot": str(args.screenshot) if args.screenshot else None,
                "robot_operations": [],
                "remaining": [
                    "deploy the extended collector into the intended robot installation",
                    "real permissions and outcomes of the five newly added plugins",
                    "motion-load overhead and long-running stability",
                ],
            }
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
            print(
                json.dumps(
                    {
                        "verified": True,
                        "preview": central + f"/robots/{identity}/diagnostics",
                        "relay": relay,
                        "record": str(args.output),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            deadline = time.monotonic() + max(0, args.preview_seconds)
            while time.monotonic() < deadline:
                time.sleep(1)
        finally:
            for child in reversed(children):
                if child.poll() is None:
                    child.send_signal(signal.SIGINT)
                    try:
                        child.wait(timeout=12)
                    except subprocess.TimeoutExpired:
                        child.terminate()
                        child.wait(timeout=5)
            for log in logs:
                log.close()


if __name__ == "__main__":
    main()
