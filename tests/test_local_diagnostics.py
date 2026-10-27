import io
import json
import sqlite3
import zipfile
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from rp1_monitor.aggregation import Accumulator
from rp1_monitor.local_operations import register_local_operations
from rp1_monitor.profiles import RobotProfile


def test_local_diagnostics_survive_restart_and_reject_cross_origin(tmp_path):
    accumulator = Accumulator("REPLAY-01", "simulated")
    accumulator.observe(
        "motors.health", {"joint.0.motor_temperature": -5, "joint.0.fault_code": 0xABCD}
    )

    def runtime():
        return SimpleNamespace(
            settings=SimpleNamespace(state_dir=tmp_path),
            source=SimpleNamespace(),
            profile=RobotProfile(robot_id="REPLAY-01"),
            snapshot=accumulator.preview,
        )

    def app():
        result = FastAPI()
        register_local_operations(result, runtime())
        return result

    with TestClient(app()) as client:
        assert (
            client.get(
                "/api/v1/doctor", headers={"Origin": "https://untrusted.invalid"}
            ).status_code
            == 403
        )
        doctor = client.get("/api/v1/doctor")
        assert doctor.status_code == 200
        assert doctor.json()["validation_scope"] == "simulated"
        response = client.post("/api/v1/diagnostics")
        assert response.status_code == 201
        saved = response.json()
    with TestClient(app()) as client:
        assert (
            client.get("/api/v1/diagnostics").json()["items"][0]["report_id"] == saved["report_id"]
        )
        response = client.get(f"/api/v1/diagnostics/{saved['report_id']}/bundle")
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            assert json.loads(archive.read("report.json"))["robot_id"] == "REPLAY-01"
        assert client.get("/api/v1/diagnostics/unknown/bundle").status_code == 404
        with sqlite3.connect(tmp_path / "diagnostics.sqlite") as db:
            db.execute("UPDATE reports SET payload='{}'")
        assert client.get(f"/api/v1/diagnostics/{saved['report_id']}/bundle").status_code == 409
