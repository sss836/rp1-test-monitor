"""Robot repository regression tests; all observations are explicitly simulated."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from rp1_robot import cli
from rp1_robot.agent import AgentSettings, create_agent_app


@pytest.mark.parametrize("command", ["desktop", "station", "server", "relay", "ask", "demo"])
def test_robot_entry_rejects_computer_commands(monkeypatch, command):
    monkeypatch.setattr(sys, "argv", ["rp1-observer", command])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2


def test_profile_creation_is_private_and_never_overwrites(monkeypatch, tmp_path):
    output = tmp_path / "profile.json"
    monkeypatch.setattr(sys, "argv", ["rp1-observer", "profile-init", "--robot-id", "SIM-PROFILE", "--output", str(output)])
    cli.main()
    assert output.stat().st_mode & 0o777 == 0o600
    assert json.loads(output.read_text())["robot_id"] == "SIM-PROFILE"
    with pytest.raises(FileExistsError):
        cli.main()


def test_simulated_api_keeps_robot_identity_and_no_workstation_routes(tmp_path):
    app = create_agent_app(AgentSettings("SIM-ROBOT", "simulated", tmp_path, reserve_bytes=0))
    with TestClient(app) as client:
        frame = client.get("/api/v1/state").json()
        assert frame["source_kind"] == "simulated"
        assert frame["robot_id"] == "SIM-ROBOT"
        assert frame["upload"]["stage"] == "robot_buffer"
        assert client.get("/api/v1/transfer/windows").status_code == 200
        assert client.get("/api/v1/doctor").status_code == 200
        assert client.get("/api/v1/board-diagnostics").status_code == 200
        assert client.get("/api/v1/state", headers={"Origin": "https://untrusted.invalid"}).status_code == 403
        for path in ("/", "/api/v1/assistant/models", "/api/v1/station", "/recordings"):
            assert client.get(path).status_code == 404


def test_deployment_does_not_contain_computer_modules():
    import rp1_robot

    package = Path(rp1_robot.__file__).parent
    for module in ("desktop", "station", "relay", "backend", "assistant", "model_providers"):
        assert importlib.util.find_spec(f"rp1_robot.{module}") is None
    assert not (package / "web").exists()


def test_robot_startup_is_manual_and_excludes_cpu_zero():
    script = Path(__file__).parents[1] / "scripts/start-robot.sh"
    text = script.read_text()
    assert "taskset --cpu-list 1-3,7" in text
    assert "-m rp1_robot" in text
    assert "flock --nonblock" in text
    assert "systemctl enable" not in text
