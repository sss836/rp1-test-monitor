#!/usr/bin/env python3
"""Actual local processes; disposable simulated devices only, no robot connections."""

import argparse
import json
import os
import secrets
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Thread

import httpx


def ports(count):
    sockets = []
    try:
        for _ in range(count):
            handle = socket.socket()
            handle.bind(("127.0.0.1", 0))
            sockets.append(handle)
        return [handle.getsockname()[1] for handle in sockets]
    finally:
        for handle in sockets:
            handle.close()


def stop(process):
    if process.poll() is None:
        process.send_signal(signal.SIGINT)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.terminate()
            process.wait(timeout=5)


def until(check, timeout=25):
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        try:
            result = check()
            if result:
                return result
        except (httpx.HTTPError, KeyError, ValueError):
            pass
        time.sleep(0.2)
    raise AssertionError("local simulated process check timed out")


def run(root, screenshot):
    started = time.monotonic()
    server_port, *endpoints = ports(11)
    agent_ports, relay_ports = endpoints[:5], endpoints[5:]
    wire_available = Event()
    wire_available.set()

    class WireProxy(BaseHTTPRequestHandler):
        """Loopback substitute for unplugging the cable; never touches real network settings."""

        def forward(self):
            if not wire_available.is_set():
                self.send_error(503, "simulated cable disconnected")
                return
            connection = HTTPConnection("127.0.0.1", agent_ports[0], timeout=2)
            try:
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                connection.request(
                    self.command, self.path, body, {"Content-Type": "application/json"}
                )
                response = connection.getresponse()
                content = response.read()
                self.send_response(response.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
            finally:
                connection.close()

        do_GET = forward
        do_POST = forward

        def log_message(self, *_):
            pass

    wire_proxy = ThreadingHTTPServer(("127.0.0.1", 0), WireProxy)
    wire_thread = Thread(target=wire_proxy.serve_forever, daemon=True)
    wire_thread.start()
    url = f"http://127.0.0.1:{server_port}"
    devices = [
        {
            "robot_id": f"E2E-SIM-{index}",
            "name": f"Simulated {index}",
            "source_kind": "simulated",
            "token": secrets.token_urlsafe(32),
        }
        for index in range(5)
    ]
    config = root / "devices.json"
    config.write_text(json.dumps(devices))
    config.chmod(0o600)
    viewer = root / "viewer.txt"
    viewer.write_text(secrets.token_urlsafe(32))
    viewer.chmod(0o600)
    database = root / "monitor.sqlite"
    children, handles, collectors, relays = [], [], [], []
    environment = {
        key: value for key, value in os.environ.items() if key not in {"PYTHONPATH", "PYTHONHOME"}
    }
    environment["QT_QPA_PLATFORM"] = "offscreen"

    def launch(name, args):
        handle = (root / f"{name}.log").open("a")
        handles.append(handle)
        child = subprocess.Popen(
            [sys.executable, "-m", "rp1_monitor", *args],
            stdout=handle,
            stderr=subprocess.STDOUT,
            env=environment,
        )
        children.append(child)
        return child

    def backend():
        return launch(
            "backend",
            [
                "server",
                "--devices",
                str(config),
                "--database-url",
                f"sqlite:///{database}",
                "--viewer-token-file",
                str(viewer),
                "--port",
                str(server_port),
            ],
        )

    try:
        server = backend()
        with httpx.Client(
            timeout=2, trust_env=False, headers={"Authorization": "Bearer " + viewer.read_text()}
        ) as client:
            until(lambda: client.get(url + "/health").status_code == 200)
            for index, device in enumerate(devices):
                token = root / f"token-{index}.txt"
                token.write_text(device["token"])
                token.chmod(0o600)
                collectors.append(
                    launch(
                        f"agent-{index}",
                        [
                            "agent",
                            "--source",
                            "simulated",
                            "--robot-id",
                            device["robot_id"],
                            "--state-dir",
                            str(root / f"agent-{index}"),
                            "--port",
                            str(agent_ports[index]),
                        ],
                    )
                )
                relays.append(
                    launch(
                        f"relay-{index}",
                        [
                            "relay",
                            "--source",
                            "simulated",
                            "--robot-id",
                            device["robot_id"],
                            "--collector",
                            f"http://127.0.0.1:{wire_proxy.server_port if index == 0 else agent_ports[index]}",
                            "--backend",
                            url,
                            "--token-file",
                            str(token),
                            "--state-dir",
                            str(root / f"relay-{index}"),
                            "--port",
                            str(relay_ports[index]),
                        ],
                    )
                )

            def states():
                response = client.get(url + "/api/v1/robot-monitors")
                response.raise_for_status()
                return response.json()["items"]

            def matching(predicate):
                items = states()
                return items if predicate(items) else False

            ready = until(
                lambda: matching(
                    lambda items: len(items) == 5 and all(item["data_fresh"] for item in items)
                )
            )
            assert all(item["health"]["battery"]["soc"] is not None for item in ready)
            assert all(item["health"]["battery"]["temperature"] is not None for item in ready)
            assert all(item["health"]["host"]["max_temperature"] is not None for item in ready)
            assert all(item["health"]["motors"]["available"] == 24 for item in ready)
            assert all(item["health"]["overall"]["state"] == "observing" for item in ready)
            assert all(len(item["health"]["processes"]["items"]) == 2 for item in ready)
            assert all(
                item["health"]["processes"]["items"][0]["rss_mib"] is not None for item in ready
            )
            local_processes = client.get(f"http://127.0.0.1:{relay_ports[0]}/api/v1/state").json()[
                "health"
            ]["processes"]["items"]
            assert len(local_processes) == 2 and local_processes[0]["state"] == "running"
            first_capture = ready[0]["captured_at"]
            desktop_args = [
                "desktop",
                "--url",
                f"http://127.0.0.1:{relay_ports[0]}",
                "--quit-after",
                "4",
            ]
            if screenshot:
                desktop_args += ["--screenshot", str(screenshot.resolve())]
            desktop = launch("desktop", desktop_args)
            assert desktop.wait(timeout=15) == 0
            first_capture = states()[0]["captured_at"]
            after_close = until(
                lambda: matching(lambda items: items[0]["captured_at"] > first_capture)
            )
            before_outage = after_close[0]["captured_at"]
            stop(server)
            time.sleep(3.2)
            agent_url = f"http://127.0.0.1:{relay_ports[0]}/api/v1/state"
            pending = client.get(agent_url).json()["upload"]["pending_windows"]
            assert pending >= 2
            server = backend()
            recovered = until(
                lambda: matching(
                    lambda items: (
                        all(item["data_fresh"] for item in items)
                        and items[0]["captured_at"] > before_outage
                    )
                ),
                timeout=35,
            )
            until(
                lambda: client.get(agent_url).json()["upload"]["pending_windows"] <= 1, timeout=35
            )
            # The collector keeps running while only the PC-to-robot link is interrupted.
            wire_available.clear()
            until(lambda: not client.get(agent_url).json()["upload"]["collector_connected"])
            local = client.get(agent_url).json()
            assert local["health"]["battery"]["soc"] is None
            assert all(
                p["state"] == "unknown" and p["rss_mib"] is None
                for p in local["health"]["processes"]["items"]
            )
            robot_url = f"http://127.0.0.1:{agent_ports[0]}/api/v1/state"
            until(lambda: client.get(robot_url).json()["upload"]["pending_windows"] >= 3)
            until(
                lambda: matching(
                    lambda items: (
                        not items[0]["online"] and all(item["online"] for item in items[1:])
                    )
                ),
                timeout=12,
            )
            wire_available.set()
            until(
                lambda: matching(lambda items: all(item["data_fresh"] for item in items)),
                timeout=25,
            )
            until(lambda: client.get(robot_url).json()["upload"]["pending_windows"] <= 1)
            until(lambda: client.get(agent_url).json()["upload"]["pending_windows"] <= 1)

            def no_holes():
                with sqlite3.connect(database) as connection:
                    low, high, count = connection.execute(
                        "SELECT min(sequence),max(sequence),count(*) FROM monitor_windows WHERE robot_id=?",
                        (devices[0]["robot_id"],),
                    ).fetchone()
                return low == 1 and count == high - low + 1

            until(no_holes)
            # Stop only our fifth simulated observer and verify stale state, while the rest continue.
            stop(collectors[4])
            stale = until(
                lambda: matching(
                    lambda items: (
                        not items[4]["online"] and all(item["online"] for item in items[:4])
                    )
                ),
                timeout=12,
            )
            assert stale[4]["health"]["battery"]["soc"] is None
            assert stale[4]["health"]["host"]["max_temperature"] is None
            assert stale[4]["health"]["motors"]["available"] == 0
            assert stale[4]["health"]["overall"]["state"] == "incomplete"
            assert all(
                p["state"] == "unknown" and p["rss_mib"] is None
                for p in stale[4]["health"]["processes"]["items"]
            )
            with sqlite3.connect(database) as connection:
                rows, average = connection.execute(
                    "SELECT count(*),avg(length(payload)) FROM monitor_windows"
                ).fetchone()
            return {
                "run_at_utc": datetime.now(timezone.utc).isoformat(),
                "wall_seconds": round(time.monotonic() - started, 2),
                "source": "simulated_only",
                "topology": "robot_collector_to_upper_computer_relay_to_central",
                "robot_central_credentials": False,
                "simulated_wire_outage_robot_cache_recovered_without_sequence_holes": True,
                "wire_outage_hides_local_health": True,
                "database": "SQLite for TCP process test; PostgreSQL covered by check_postgres.py",
                "robots": 5,
                "all_fresh_initially": len(ready) == 5,
                "health_battery_host_24motors_reached_central": True,
                "offline_health_values_invalidated": True,
                "core_processes_reached_relay_and_central": True,
                "offline_process_states_and_resources_invalidated": True,
                "desktop_exit_code": desktop.returncode,
                "upload_continued_after_desktop_closed": after_close[0]["captured_at"]
                > first_capture,
                "pending_during_backend_outage": pending,
                "all_fresh_after_recovery": all(item["data_fresh"] for item in recovered),
                "offline_detected_without_affecting_other_robots": not stale[4]["online"],
                "stored_windows": rows,
                "average_json_bytes_per_window": round(average),
                "estimated_30day_json_gib_one_robot": round(average * 86400 * 30 / 1024**3, 2),
                "note": "Capacity excludes table/index/WAL/backups; this is not hardware or one-hour endurance acceptance.",
            }
    finally:
        wire_proxy.shutdown()
        wire_proxy.server_close()
        wire_thread.join(timeout=2)
        for child in reversed(children):
            stop(child)
        for handle in handles:
            handle.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path)
    parser.add_argument("--screenshot", type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="rp1-monitor-e2e-") as temporary:
        result = run(Path(temporary), args.screenshot)
    output = json.dumps(result, ensure_ascii=False, indent=2)
    print(output)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(output + "\n")
