"""Scope and original-evidence checks for temperature details and host config sync."""

import time
from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy import insert, select

from rp1_monitor.aggregation import Accumulator
from rp1_monitor.central_policy import CentralPolicy
from rp1_monitor.central_tables import alert_events
from rp1_monitor.models import MetricRollup
from rp1_monitor.monitor_config import empty_configuration
from rp1_monitor.relay import RelayRuntime, RelaySettings
from rp1_monitor.storage import history


def test_temperature_detail_filters_events_and_agent_uses_same_evidence(
    client, window, write_headers, read_headers
):
    store = client.app.state.store
    store.policy = CentralPolicy(
        alert_hold_seconds=0, temperature_limits_c={"joint.27.motor_temperature": 60}
    )
    sample = window()
    sample.quality.joint_names = [f"joint_{i}" for i in range(28)]
    for key, value in (("joint.27.motor_temperature", 85), ("joint.27.mos_temperature", 45)):
        sample.metrics[key] = MetricRollup(
            last=value, minimum=value, maximum=value, count=1, age_ms=1
        )
        sample.quality.health_values[key] = value
        sample.quality.health_ages_ms[key] = 1
    assert (
        client.post(
            "/api/v1/robot-telemetry/batches",
            json={"delivery": "live", "windows": [sample.model_dump(mode="json")]},
            headers=write_headers,
        ).status_code
        == 200
    )
    store.alert_monitor.tick()
    with store.engine.begin() as connection:
        raw = connection.execute(select(history.c.payload, history.c.payload_hash)).all()
        for _ in range(25):
            connection.execute(
                insert(alert_events).values(
                    robot_id="RP1-TEST",
                    rule_key="limit.joint.0.motor_temperature",
                    occurred_at=datetime.now(timezone.utc),
                    event_kind="observation_lost",
                    policy_sha256=store.policy.digest,
                    evidence={"detail": "其他关节的模拟噪声"},
                )
            )
    url = "/api/v1/robot-monitors/RP1-TEST/joints/27/temperature"
    assert client.get(url).status_code == 401
    response = client.get(url, headers=read_headers)
    assert response.status_code == 200
    data = response.json()
    assert data["joint"] == 27 and data["current"]["state"] == "hot"
    assert len(data["curves"]) == 2 and data["curves"][0]["points"][-1]["last"] == 85
    assert data["alerts"]["events"] and all(
        event["rule_key"].startswith("limit.joint.27.") for event in data["alerts"]["events"]
    )
    assert any(head["opened_at"] for head in data["alerts"]["heads"])
    answer = client.post(
        "/api/v1/robot-monitors/RP1-TEST/assistant",
        headers=read_headers,
        json={
            "question": "核对关节27温度和恢复记录",
            "use_model": False,
            "metrics": ["joint.27.motor_temperature", "joint.27.mos_temperature"],
        },
    )
    assert answer.status_code == 200
    result = answer.json()
    assert result["mode"] == "local_rules" and "85.0" in result["answer"]
    assert result["evidence"]["E1"]["temperature_events"][0]["joint"] == 27
    assert client.get(url + "?minutes=1441", headers=read_headers).status_code == 422
    assert (
        client.get(
            "/api/v1/robot-monitors/OTHER/joints/27/temperature", headers=read_headers
        ).status_code
        == 404
    )
    with store.engine.connect() as connection:
        assert connection.execute(select(history.c.payload, history.c.payload_hash)).all() == raw


def test_relay_reads_only_bound_configuration_and_keeps_it_out_of_raw_windows(tmp_path):
    runtime = RelayRuntime(
        RelaySettings(
            "RP1-TEST",
            "simulated",
            "http://127.0.0.1:8891",
            "http://127.0.0.1:8890",
            "a" * 32,
            tmp_path,
        )
    )
    acc = Accumulator("RP1-TEST", "simulated")
    acc.observe("motors.health", {"joint.0.motor_temperature": 80})
    runtime._preview = acc.preview().model_dump(mode="json")
    runtime._preview["monitoring_configuration"] = {"source": "database", "robot_id": "UNTRUSTED"}
    runtime._received_at = time.monotonic()
    assert runtime.snapshot().monitoring_configuration is None
    configuration = empty_configuration(28).model_dump()
    configuration["temperature_limits_c"] = {"joint.0.motor_temperature": 90}
    envelope = {
        "robot_id": "RP1-TEST",
        "source_kind": "simulated",
        "revision": 7,
        "source": "database",
        "configuration": configuration,
    }

    def central(request):
        assert str(request.url) == "http://127.0.0.1:8890/api/v1/robot-telemetry/configuration"
        assert request.headers["authorization"] == "Bearer " + "a" * 32
        return httpx.Response(200, json=envelope)

    with httpx.Client(transport=httpx.MockTransport(central)) as connection:
        runtime.sync_configuration(connection)
        frame = runtime.snapshot()
        assert frame.monitoring_configuration["revision"] == 7
        assert frame.upload["central_url"] == "http://127.0.0.1:8890"
        envelope["robot_id"] = "OTHER"
        with pytest.raises(ValueError, match="绑定不一致"):
            runtime.sync_configuration(connection)
    assert runtime.snapshot().monitoring_configuration["robot_id"] == "RP1-TEST"
    assert "monitoring_configuration" not in acc.flush().model_dump()
