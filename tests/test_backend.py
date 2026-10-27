import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from rp1_monitor.models import TelemetryBatch, TelemetryWindow
from rp1_monitor.storage import history

PATH = "/api/v1/robot-telemetry/batches"


def send(client, windows, headers, delivery="live"):
    return client.post(
        PATH,
        json={"delivery": delivery, "windows": [item.model_dump(mode="json") for item in windows]},
        headers=headers,
    )


def test_authenticated_live_snapshot_and_no_credential_leak(
    client, window, write_headers, read_headers
):
    assert send(client, [window()], write_headers).status_code == 200
    response = client.get("/api/v1/robot-monitors", headers=read_headers)
    item = response.json()["items"][0]
    assert item["online"] and item["data_fresh"]
    assert item["source_kind"] == "simulated"
    assert "token" not in response.text


def test_read_and_write_credentials_are_separate(client, window, write_headers, read_headers):
    assert send(client, [window()], read_headers).status_code == 401
    assert client.get("/api/v1/robot-monitors", headers=write_headers).status_code == 401
    assert client.get("/api/v1/robot-monitors").status_code == 401


def test_device_cannot_impersonate_robot_or_source(client, window, write_headers):
    assert send(client, [window(robot_id="OTHER")], write_headers).status_code == 403
    assert send(client, [window(source_kind="ros2")], write_headers).status_code == 403


def test_idempotency_and_conflict_are_transactional(client, window, write_headers):
    first = window()
    assert send(client, [first], write_headers).json()["inserted"] == 1
    assert send(client, [first], write_headers).json()["duplicates"] == 1
    altered = window(quality={"invalid_values": 2})
    altered = TelemetryWindow.model_validate(
        {
            **altered.model_dump(mode="json"),
            "window_start": first.window_start,
            "window_end": first.window_end,
        }
    )
    second = window(sequence=2)
    assert send(client, [second, altered], write_headers).status_code == 409
    with client.app.state.store.engine.connect() as connection:
        assert connection.execute(select(func.count()).select_from(history)).scalar_one() == 1


def test_backfill_and_out_of_order_never_overwrite_latest(
    client, window, write_headers, read_headers
):
    new = window(sequence=20)
    old = window(sequence=19, offset=-60)
    send(client, [new], write_headers)
    send(client, [old], write_headers, "backfill")
    send(client, [window(sequence=18, offset=-10)], write_headers)
    state = client.get("/api/v1/robot-monitors", headers=read_headers).json()["items"][0]
    assert state["captured_at"] == new.model_dump(mode="json")["window_end"]


def test_restart_sequence_reset_and_delayed_previous_stream(
    client, window, write_headers, read_headers
):
    old = window(sequence=100)
    send(client, [old], write_headers)
    new = window(sequence=1, stream_id=uuid4(), offset=0)
    send(client, [new], write_headers)
    send(client, [window(sequence=101, stream_id=old.stream_id, offset=0.1)], write_headers)
    state = client.get("/api/v1/robot-monitors", headers=read_headers).json()["items"][0]
    assert state["captured_at"] == new.model_dump(mode="json")["window_end"]


def test_collector_online_is_separate_from_stale_or_empty_data(
    client, window, write_headers, read_headers
):
    send(
        client,
        [window(quality={"topic_ages_ms": {"/joint_states": 9000, "/action": 9000, "/imu": 9000}})],
        write_headers,
    )
    state = client.get("/api/v1/robot-monitors", headers=read_headers).json()["items"][0]
    assert state["online"] and not state["data_fresh"]
    send(client, [window(sequence=2, metrics={})], write_headers)
    state = client.get("/api/v1/robot-monitors", headers=read_headers).json()["items"][0]
    assert state["online"] and not state["data_fresh"]


def test_old_live_frame_is_not_fresh_at_server(client, window, write_headers, read_headers):
    send(client, [window(offset=-30)], write_headers)
    state = client.get("/api/v1/robot-monitors", headers=read_headers).json()["items"][0]
    assert not state["data_fresh"] and state["clock_unverified"]


def test_history_preserves_spike_and_rejects_unbounded_query(
    client, window, write_headers, read_headers
):
    send(client, [window()], write_headers)
    response = client.get(
        "/api/v1/robot-monitors/RP1-TEST/telemetry?metric=joint.0.position", headers=read_headers
    )
    assert response.status_code == 200
    assert response.json()["points"][0]["maximum"] == 99
    assert response.json()["kind"] == "one_second_summary"
    assert (
        client.get(
            "/api/v1/robot-monitors/RP1-TEST/telemetry?metric=joint.0.position&max_points=100000",
            headers=read_headers,
        ).status_code
        == 422
    )
    assert (
        client.get(
            "/api/v1/robot-monitors/RP1-TEST/telemetry?metric=arbitrary.sql", headers=read_headers
        ).status_code
        == 422
    )
    assert (
        client.get(
            "/api/v1/robot-monitors/RP1-TEST/telemetry?metric=joint.0.position&start=2026-01-01T00:00:00",
            headers=read_headers,
        ).status_code
        == 422
    )


def test_history_reports_missing_windows_and_gap(client, window, write_headers, read_headers):
    now = datetime.now(timezone.utc)
    windows = []
    for index in range(12):
        end = now - timedelta(seconds=30 - index)
        payload = window(sequence=index + 1).model_dump(mode="json")
        payload.update(window_start=end - timedelta(seconds=1), window_end=end)
        if index == 4:
            payload["metrics"]["joint.0.position"] = {"last": None, "count": 0}
        if index == 6:
            continue
        windows.append(TelemetryWindow.model_validate(payload))
    send(client, windows, write_headers, "backfill")
    response = client.get(
        "/api/v1/robot-monitors/RP1-TEST/telemetry?metric=joint.0.position&max_points=10",
        headers=read_headers,
    ).json()
    assert len(response["points"]) <= 10
    assert sum(point["missing_windows"] for point in response["points"]) == 1
    assert sum(point["gap_count"] for point in response["points"]) >= 1


def test_finite_values_and_payload_limits(client, window, write_headers):
    payload = window().model_dump(mode="json")
    payload["metrics"]["joint.0.position"]["last"] = float("nan")
    raw = json.dumps({"delivery": "live", "windows": [payload]})
    assert (
        client.post(
            PATH, content=raw, headers={**write_headers, "Content-Type": "application/json"}
        ).status_code
        == 422
    )
    assert (
        client.post(
            PATH,
            content=b"x" * (1024 * 1024 + 1),
            headers={**write_headers, "Content-Type": "application/json"},
        ).status_code
        == 413
    )
    assert client.post(PATH, content=b"{}", headers=write_headers).status_code == 415
    with pytest.raises(ValidationError):
        TelemetryBatch.model_validate({"delivery": "live", "windows": []})


def test_removed_device_is_revoked_without_deleting_history(client, window, write_headers):
    send(client, [window()], write_headers)
    client.app.state.store.configure([])
    assert send(client, [window(sequence=2)], write_headers).status_code == 401
    with client.app.state.store.engine.connect() as connection:
        assert connection.execute(select(func.count()).select_from(history)).scalar_one() == 1


def test_unknown_stale_stream_cannot_overwrite_live(client, window, write_headers, read_headers):
    current = window()
    send(client, [current], write_headers)
    response = send(client, [window(stream_id=uuid4(), offset=-172800)], write_headers)
    assert response.json()["live_ignored"] == 1
    state = client.get("/api/v1/robot-monitors", headers=read_headers).json()["items"][0]
    assert state["captured_at"] == current.model_dump(mode="json")["window_end"]


def test_demo_cannot_anonymously_reopen_database_with_ros_history(tmp_path):
    from rp1_monitor.backend import BackendSettings, create_app
    from rp1_monitor.storage import Store

    url = f"sqlite:///{tmp_path / 'mixed.sqlite'}"
    store = Store(url)
    store.configure(
        [
            {
                "robot_id": "ROS-DEVICE",
                "name": "isolated fake ROS device",
                "source_kind": "ros2",
                "token": "r" * 40,
            }
        ]
    )
    store.engine.dispose()
    config = tmp_path / "demo.json"
    config.write_text(
        json.dumps(
            [
                {
                    "robot_id": "SIM-DEVICE",
                    "name": "simulated",
                    "source_kind": "simulated",
                    "token": "s" * 40,
                }
            ]
        )
    )
    with pytest.raises(ValueError, match="禁止复用"):
        create_app(BackendSettings(url, config, demo_read=True))


def test_range_metrics_cannot_hide_unknown_extrema(window):
    payload = window().model_dump(mode="json")
    payload["metrics"]["joint.0.position"].update(minimum=None, maximum=None)
    with pytest.raises(ValidationError, match="必须提供极值"):
        TelemetryWindow.model_validate(payload)


def test_revocation_between_auth_and_transaction_is_rechecked(client, window, write_headers):
    from rp1_monitor.storage import token_digest

    store = client.app.state.store
    actor = store.device_for_digest(token_digest(write_headers["Authorization"][7:]))
    store.configure([])
    with pytest.raises(PermissionError, match="已撤销"):
        store.ingest(TelemetryBatch(delivery="live", windows=[window()]), actor)


def test_individual_stale_metric_prevents_all_data_fresh(
    client, window, write_headers, read_headers
):
    payload = window().model_dump(mode="json")
    payload["metrics"]["joint.0.position"]["age_ms"] = 3600000
    send(client, [TelemetryWindow.model_validate(payload)], write_headers)
    item = client.get("/api/v1/robot-monitors", headers=read_headers).json()["items"][0]
    assert item["online"] and not item["data_fresh"]


def test_ros_mapping_change_cannot_mix_physical_history(client, window):
    from rp1_monitor.storage import IngestConflict, token_digest

    store = client.app.state.store
    store.configure(
        [
            {
                "robot_id": "ROS-MAPPING",
                "name": "Fake ROS mapping",
                "source_kind": "ros2",
                "token": "m" * 40,
            }
        ]
    )
    actor = store.device_for_digest(token_digest("m" * 40))
    first = window(
        robot_id="ROS-MAPPING", source_kind="ros2", quality={"joint_names": ["hip", "knee"]}
    )
    second = window(
        robot_id="ROS-MAPPING",
        source_kind="ros2",
        stream_id=uuid4(),
        quality={"joint_names": ["hip", "ankle"]},
    )
    store.ingest(TelemetryBatch(delivery="backfill", windows=[first]), actor)
    with pytest.raises(IngestConflict, match="关节规范名单变化"):
        store.ingest(TelemetryBatch(delivery="backfill", windows=[second]), actor)
