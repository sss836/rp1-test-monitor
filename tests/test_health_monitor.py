import hashlib
import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update

from rp1_monitor import aggregation
from rp1_monitor.aggregation import Accumulator
from rp1_monitor.health_view import build_health
from rp1_monitor.models import MetricRollup
from rp1_monitor.storage import history


def test_health_polling_has_own_freshness_and_preserves_window_facts(monkeypatch):
    now = [1_000_000_000]
    monkeypatch.setattr(aggregation.time, "monotonic_ns", lambda: now[0])
    accumulator = Accumulator("TEST", "ros2")
    accumulator.observe("/joint_states", {"joint.0.position": 1})
    accumulator.observe("bms.journal", {"battery.soc": 82.5})
    now[0] += 1_000_000
    accumulator.flush()
    now[0] = 3_000_000_000
    frame = accumulator.preview()
    assert frame.values["joint.0.position"] is None
    assert frame.values["battery.soc"] == 82.5
    window = accumulator.flush()
    assert window.metrics["battery.soc"].count == 0
    assert window.metrics["battery.soc"].last is None
    assert window.quality.health_values["battery.soc"] == 82.5
    assert window.quality.health_ages_ms["battery.soc"] == 2000
    now[0] += 4_000_000_000
    assert accumulator.preview().values["battery.soc"] is None


def test_missing_health_never_becomes_healthy_or_zero():
    health = build_health({"joint.0.position": 0}, {"joint.0.position": 1}, {}, {})
    assert health["overall"]["state"] == "incomplete"
    assert health["battery"]["soc"] is None
    assert health["host"]["max_temperature"] is None
    assert health["motors"]["available"] == 0
    assert health["motors"]["items"][0]["fault_code"] is None


def test_motor_coverage_includes_known_roster_even_without_any_samples():
    names = [f"joint_{index}" for index in range(24)]
    values = {"joint.0.motor_temperature": 42, "joint.0.fault_code": 0}
    health = build_health(values, {key: 1 for key in values}, {}, {}, names)
    assert health["motors"]["available"] == 1 and health["motors"]["total"] == 24
    assert health["motors"]["state"] == "incomplete"
    assert health["motors"]["items"][23]["temperature"] is None
    empty = build_health({}, {}, {}, {}, names)
    assert empty["motors"]["available"] == 0 and empty["motors"]["total"] == 24
    assert empty["overall"]["state"] == "incomplete"


def test_raw_fault_and_protection_are_reported_without_invented_thresholds():
    values = {
        "joint.0.motor_temperature": 42,
        "joint.0.fault_code": 513,
        "battery.voltage": 49,
        "battery.current": -2,
        "battery.soc": 82.5,
        "battery.protection": 1024,
    }
    health = build_health(values, {key: 2 for key in values}, {}, {})
    assert health["motors"]["faults"] == [{"joint": 0, "code": 513}]
    assert health["overall"]["state"] == "fault"
    assert health["battery"]["protection"] == 1024
    assert health["battery"]["derived_power"] == -98
    assert health["battery"]["temperature"] is None
    stale = build_health(values, {key: 6000 for key in values}, {}, {})
    assert stale["overall"]["state"] == "incomplete"
    assert stale["battery"]["derived_power"] is None
    assert stale["motors"]["faults"] == []


def test_rp_server_driver_id_is_not_ros_joint_index_and_old_snapshot_expires():
    source = {
        "rp_server": {
            "state": "live",
            "detail": "缓存快照",
            "observed_at": datetime.now(timezone.utc),
            "reported_faults": [{"motor_id": 45, "code": 513, "name": "raw"}],
        }
    }
    health = build_health({"joint.0.position": 1}, {}, source, {}, ["left_hip"])
    assert health["motors"]["faults"] == [{"joint": "驱动ID 45", "code": 513}]
    assert health["motors"]["available"] == 0
    source["rp_server"]["observed_at"] -= timedelta(seconds=6)
    assert build_health({}, {}, source, {})["motors"]["faults"] == []
    assert build_health({}, {}, source, {}, available=False)["overall"]["state"] == "incomplete"


def test_health_snapshot_reaches_central_without_faking_sample_count(
    client, window, write_headers, read_headers
):
    frame = window()
    frame.metrics["battery.soc"] = MetricRollup(last=None, count=0, age_ms=1200)
    frame.quality.health_values = {
        "battery.soc": 82.5,
        "battery.voltage": 49.2,
        "battery.current": -2,
        "battery.temperature": 31,
    }
    frame.quality.health_ages_ms = {key: 1200 for key in frame.quality.health_values}
    result = client.post(
        "/api/v1/robot-telemetry/batches",
        json={"delivery": "live", "windows": [frame.model_dump(mode="json")]},
        headers=write_headers,
    )
    assert result.status_code == 200
    state = client.get("/api/v1/robot-monitors", headers=read_headers).json()["items"][0]
    assert state["data_fresh"]  # Slow health polling must not invalidate fast telemetry.
    assert state["health"]["battery"]["soc"] == 82.5
    assert state["health"]["battery"]["temperature"] == 31
    assert state["metrics"]["battery.soc"]["count"] == 0
    assert state["health"]["overall"]["state"] == "incomplete"
    frame = window(sequence=2)
    frame.quality.health_values = {"battery.soc": 82.5}
    frame.quality.health_ages_ms = {"battery.soc": 6000}
    client.post(
        "/api/v1/robot-telemetry/batches",
        json={"delivery": "live", "windows": [frame.model_dump(mode="json")]},
        headers=write_headers,
    )
    state = client.get("/api/v1/robot-monitors", headers=read_headers).json()["items"][0]
    assert state["health"]["battery"]["soc"] is None


def test_pre_health_record_retry_is_idempotent_without_rewriting_evidence(
    client, window, write_headers
):
    frame = window()
    body = {"delivery": "live", "windows": [frame.model_dump(mode="json")]}
    assert (
        client.post("/api/v1/robot-telemetry/batches", json=body, headers=write_headers).status_code
        == 200
    )
    old_payload = frame.model_dump(mode="json")
    for key in ("health_sources", "health_sensors", "health_values", "health_ages_ms"):
        old_payload["quality"].pop(key)
    old_hash = hashlib.sha256(
        json.dumps(old_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    store = client.app.state.store
    with store.engine.begin() as connection:
        connection.execute(update(history).values(payload=old_payload, payload_hash=old_hash))
    retry = client.post("/api/v1/robot-telemetry/batches", json=body, headers=write_headers)
    assert retry.status_code == 200 and retry.json()["duplicates"] == 1
    with store.engine.connect() as connection:
        stored = connection.execute(select(history.c.payload, history.c.payload_hash)).one()
    assert stored.payload == old_payload and stored.payload_hash == old_hash
    body["windows"][0]["quality"]["health_values"] = {"battery.soc": 80}
    assert (
        client.post("/api/v1/robot-telemetry/batches", json=body, headers=write_headers).status_code
        == 409
    )
