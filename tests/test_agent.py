import ast
from pathlib import Path

import pytest

from rp1_monitor.agent import AgentSettings
from rp1_monitor.spool import window_digest


def test_robot_transfer_requires_exact_durable_content_ack(tmp_path):
    from fastapi.testclient import TestClient

    from rp1_monitor.agent import create_agent_app

    app = create_agent_app(AgentSettings("RP1-TEST", "simulated", tmp_path))
    runtime = app.state.runtime
    runtime.accumulator.observe("/joint_states", {"joint.0.position": 99})
    window = runtime.accumulator.flush()
    runtime.spool.append(window)
    client = TestClient(app)
    response = client.get("/api/v1/transfer/windows?delivery=live")
    assert response.status_code == 200
    assert response.json()["windows"][0] == window.model_dump(mode="json")
    assert runtime.spool.status()["pending_windows"] == 1
    receipt = {
        "stream_id": str(window.stream_id),
        "sequence": window.sequence,
        "payload_sha256": "0" * 64,
    }
    body = {"robot_id": "RP1-TEST", "receipts": [receipt]}
    assert client.post("/api/v1/transfer/receipts", json=body).status_code == 409
    assert runtime.spool.status()["pending_windows"] == 1
    receipt["payload_sha256"] = window_digest(window)
    assert (
        client.post("/api/v1/transfer/receipts", json={**body, "robot_id": "OTHER"}).status_code
        == 403
    )
    assert (
        client.post(
            "/api/v1/transfer/receipts", json=body, headers={"Origin": "https://untrusted.invalid"}
        ).status_code
        == 403
    )
    assert client.post("/api/v1/transfer/receipts", json=body).status_code == 200
    assert runtime.spool.status()["pending_windows"] == 0
    assert client.post("/api/v1/transfer/receipts", json=body).status_code == 200


def test_robot_cli_rejects_old_central_upload_flags(tmp_path):
    import subprocess
    import sys

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "rp1_monitor",
            "agent",
            "--source",
            "simulated",
            "--robot-id",
            "TEST",
            "--state-dir",
            str(tmp_path),
            "--backend",
            "https://central.invalid",
        ],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 2
    assert "unrecognized arguments: --backend" in result.stderr


def test_observer_contains_no_actuation_calls():
    source = Path(__file__).parents[1] / "src/rp1_monitor/sources.py"
    tree = ast.parse(source.read_text())
    forbidden = {"create_publisher", "create_client", "call_async", "publish"}
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in forbidden
        for node in ast.walk(tree)
    )


def test_arbitrary_website_cannot_open_observer_websocket(tmp_path):
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    from rp1_monitor.agent import create_agent_app

    settings = AgentSettings("RP1-TEST", "simulated", tmp_path)
    with TestClient(create_agent_app(settings)) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(
                "/api/v1/telemetry", headers={"Origin": "https://unrelated.example"}
            ):
                pass
        with client.websocket_connect("/api/v1/telemetry") as websocket:
            assert websocket.receive_json()["source_kind"] == "simulated"
