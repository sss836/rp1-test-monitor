"""Loopback-only simulated SSH -> collector -> relay -> central integration. No real robot."""

import select
import socket
import socketserver
import threading
import time

import httpx
import paramiko
import uvicorn

from rp1_monitor.agent import AgentSettings, create_agent_app
from rp1_monitor.ssh_transport import fingerprint
from rp1_monitor.station import create_station_app


def serve(app):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, access_log=False, log_level="critical"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started:
        assert thread.is_alive() and time.monotonic() < deadline
        time.sleep(0.02)
    return server, thread, port


def unused_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_password_ssh_chain_receives_simulated_data_without_robot_commands(tmp_path):
    agent, agent_thread, collector_port = serve(create_agent_app(AgentSettings(
        "SIMULATED-SSH-ONLY", "simulated", tmp_path / "collector")))
    host_key = paramiko.RSAKey.generate(2048)
    observations = {"auth": 0, "exec": 0, "shell": 0, "forward": []}
    transports = []

    class Permission(paramiko.ServerInterface):
        def __init__(self):
            self.targets = {}

        def get_allowed_auths(self, username):
            return "password"

        def check_auth_password(self, username, password):
            observations["auth"] += 1
            return paramiko.AUTH_SUCCESSFUL if (username, password) == ("fixture", "test-only-password") else paramiko.AUTH_FAILED

        def check_channel_direct_tcpip_request(self, chanid, origin, destination):
            observations["forward"].append(destination)
            if destination != ("127.0.0.1", collector_port):
                return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED
            self.targets[chanid] = destination
            return paramiko.OPEN_SUCCEEDED

        def check_channel_exec_request(self, channel, command):
            observations["exec"] += 1
            return False

        def check_channel_shell_request(self, channel):
            observations["shell"] += 1
            return False

    def forward(channel, destination):
        with socket.create_connection(destination, timeout=3) as connection:
            try:
                while True:
                    ready, _, _ = select.select([connection, channel], [], [], 0.5)
                    if not channel.active:
                        return
                    for source in ready:
                        data = source.recv(65536)
                        if not data:
                            return
                        (channel if source is connection else connection).sendall(data)
            finally:
                try:
                    channel.close()
                except EOFError:
                    pass

    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            transport = paramiko.Transport(self.request)
            transports.append(transport)
            transport.add_server_key(host_key)
            permission = Permission()
            try:
                transport.start_server(server=permission)
                while transport.is_active():
                    channel = transport.accept(timeout=0.5)
                    if channel is not None:
                        threading.Thread(target=forward, args=(channel, permission.targets[channel.chanid]), daemon=True).start()
            except (EOFError, OSError, paramiko.SSHException):
                pass
            finally:
                transport.close()

    class SSHServer(socketserver.ThreadingTCPServer):
        daemon_threads = True

    ssh = SSHServer(("127.0.0.1", 0), Handler)
    ssh_thread = threading.Thread(target=ssh.serve_forever, daemon=True)
    ssh_thread.start()
    station_port = unused_port()
    directory = tmp_path / "station"
    app = create_station_app(directory, station_port)
    # Bind the selected workstation port, keeping all traffic on this test's loopback sockets.
    station = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=station_port, access_log=False, log_level="critical"))
    station_thread = threading.Thread(target=station.run, daemon=True)
    station_thread.start()
    deadline = time.monotonic() + 5
    while not station.started:
        assert station_thread.is_alive() and time.monotonic() < deadline
        time.sleep(0.02)
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{station_port}", trust_env=False, timeout=20) as client:
            client.get("/connect").raise_for_status()
            control = client.get("/api/v1/station").json()["control_token"]
            headers = {"X-RP1-Station-Token": control}
            body = {"target": "ssh fixture@127.0.0.1", "password": "test-only-password",
                    "ssh_port": ssh.server_address[1], "collector_port": collector_port}
            challenge = client.post("/api/v1/station/connect", json=body, headers=headers)
            assert challenge.json()["status"] == "host_key_required"
            assert challenge.json()["fingerprint"] == fingerprint(host_key)
            assert observations["auth"] == 0
            body["confirmed_fingerprint"] = challenge.json()["fingerprint"]
            bad = client.post("/api/v1/station/connect", json={**body, "password": "wrong-test-only"}, headers=headers)
            assert bad.status_code == 422 and "密码" in bad.json()["detail"]
            connected = client.post("/api/v1/station/connect", json=body, headers=headers)
            assert connected.status_code == 200, connected.text
            result = connected.json()
            assert result["robot"] == {"robot_id": "SIMULATED-SSH-ONLY", "source_kind": "simulated"}
            deadline = time.monotonic() + 12
            while True:
                robots = client.get("/api/v1/robot-monitors").json()["items"]
                if robots and robots[0].get("online") and robots[0].get("health", {}).get("battery", {}).get("soc") is not None:
                    break
                assert time.monotonic() < deadline, str(robots)[:200]
                time.sleep(0.2)
            assert robots[0]["source_kind"] == "simulated"
            preview = httpx.get(result["relay_url"] + "/api/v1/state", trust_env=False).json()
            assert preview["robot_id"] == "SIMULATED-SSH-ONLY"
            assert preview["upload"]["stage"] == "upper_computer"
            assert len([key for key in preview["values"] if key.startswith("joint.") and key.endswith(".position")]) == 24
            assert observations["exec"] == 0 and observations["shell"] == 0
            assert observations["forward"] and set(observations["forward"]) == {("127.0.0.1", collector_port)}
            assert client.post("/api/v1/station/disconnect", headers=headers).status_code == 200
            assert client.get("/api/v1/station").json()["connected"] is False
            assert client.get("/api/v1/robot-monitors").json()["items"]
        for path in directory.rglob("*"):
            if path.is_file():
                assert b"test-only-password" not in path.read_bytes()
    finally:
        station.should_exit = True
        station_thread.join(timeout=25)
        ssh.shutdown()
        ssh.server_close()
        for transport in transports:
            transport.close()
        agent.should_exit = True
        agent_thread.join(timeout=10)
