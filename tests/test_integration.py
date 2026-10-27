import json

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from rp1_monitor.backend import BackendSettings
from rp1_monitor.integration import mount_monitoring
from rp1_monitor.storage import Store


def test_same_origin_mount_reuses_human_session_but_not_device_token(tmp_path):
    config = tmp_path / "devices.json"
    config.write_text(
        json.dumps(
            [
                {
                    "robot_id": "MOUNT-SIM",
                    "name": "Mounted simulated",
                    "source_kind": "simulated",
                    "token": "d" * 40,
                }
            ]
        )
    )
    url = f"sqlite:///{tmp_path / 'monitor.sqlite'}"
    initialized = Store(url)
    initialized.engine.dispose()
    host = FastAPI()

    def human_read(request: Request):
        if request.headers.get("authorization") or request.cookies.get("test-session") != "human":
            raise HTTPException(401, "human session required")

    child = mount_monitoring(
        host, BackendSettings(url, config, initialize=False), read_dependency=human_read
    )
    with TestClient(host) as client:
        assert child.state.store.alert_monitor.thread.is_alive()
        assert client.get("/monitor/").status_code == 200
        assert client.get("/monitor/assets/app.js").status_code == 200
        assert client.get("/monitor/api/v1/robot-monitors").status_code == 401
        assert (
            client.get(
                "/monitor/api/v1/robot-monitors", headers={"Authorization": "Bearer " + "d" * 40}
            ).status_code
            == 401
        )
        client.cookies.set("test-session", "human")
        assert client.get("/monitor/api/v1/robot-monitors").status_code == 200
        assert client.post("/monitor/api/v1/robot-telemetry/batches", json={}).status_code == 401
        assert client.get("/api/v1/ingestion/anything").status_code == 404
    child.state.store.engine.dispose()


def test_mount_refuses_automatic_migration():
    with pytest.raises(ValueError, match="禁止自动建表"):
        mount_monitoring(
            FastAPI(), BackendSettings("sqlite://", None), read_dependency=lambda: None
        )
