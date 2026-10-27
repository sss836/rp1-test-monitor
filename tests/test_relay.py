from uuid import uuid4

import httpx
import pytest

from rp1_monitor.models import BatchAck
from rp1_monitor.relay import RelayRuntime, RelaySettings


def runtime_for(tmp_path, backend="http://127.0.0.1:8890"):
    runtime = RelayRuntime(
        RelaySettings("RP1-TEST", "simulated", "http://127.0.0.1:8891", backend, "a" * 32, tmp_path)
    )
    runtime.current_stream_id = uuid4()
    return runtime


def robot_fixture(tmp_path):
    from fastapi.testclient import TestClient

    from rp1_monitor.agent import AgentSettings, create_agent_app

    app = create_agent_app(AgentSettings("RP1-TEST", "simulated", tmp_path / "robot"))
    robot = app.state.runtime
    for value in (2, 99, 3):
        robot.accumulator.observe("/joint_states", {"joint.0.position": value})
    robot.accumulator.observe("bms.journal", {"battery.soc": 80})
    from rp1_monitor.process_monitor import simulate_processes

    simulate_processes(robot.accumulator, robot.profile.processes, 10)
    frame = robot.accumulator.flush()
    robot.spool.append(frame)
    api = TestClient(app)
    receipts = []

    def route(request):
        assert "authorization" not in request.headers  # Central credentials stay on the PC.
        if request.method == "POST":
            receipts.append(request.content)
        response = api.request(
            request.method,
            str(request.url),
            content=request.content,
            headers={"Content-Type": "application/json"},
        )
        return httpx.Response(response.status_code, content=response.content)

    return robot, frame, httpx.MockTransport(route), receipts


def test_two_hop_transfer_survives_pc_restart_and_preserves_source_evidence(
    tmp_path, client, write_headers, read_headers
):
    from sqlalchemy import select

    from rp1_monitor.models import TelemetryWindow
    from rp1_monitor.storage import history

    robot, original, transport, receipts = robot_fixture(tmp_path)
    pc = runtime_for(tmp_path / "pc")
    with httpx.Client(transport=transport) as connection:
        pc.receive_once(connection)
    assert robot.spool.status()["pending_windows"] == 0
    assert len(receipts) == 1
    assert pc.spool.oldest() == [original]
    assert pc.spool.oldest()[0].metrics["joint.0.position"].maximum == 99
    assert pc.snapshot().health["processes"]["items"][0]["rss_mib"] is not None
    # Restart with no robot connection: persist and upload the original window as history.
    restarted = runtime_for(tmp_path / "pc")
    restarted.current_stream_id = None
    restarted.settings.token = write_headers["Authorization"][7:]

    def central(request):
        response = client.post(
            "/api/v1/robot-telemetry/batches",
            content=request.content,
            headers={**write_headers, "Content-Type": "application/json"},
        )
        return httpx.Response(response.status_code, content=response.content)

    with httpx.Client(transport=httpx.MockTransport(central)) as connection:
        restarted.upload_once(connection)
    assert restarted.spool.status()["pending_windows"] == 0
    with client.app.state.store.engine.connect() as connection:
        payload = connection.execute(select(history.c.payload)).scalar_one()
    assert TelemetryWindow.model_validate(payload) == original
    assert (
        client.get("/api/v1/robot-monitors", headers=read_headers).json()["items"][0]["online"]
        is False
    )


def test_pc_disk_failure_never_acknowledges_robot(tmp_path, monkeypatch):
    robot, original, transport, receipts = robot_fixture(tmp_path)
    pc = runtime_for(tmp_path / "pc")

    def full(_):
        raise OSError("disk full")

    monkeypatch.setattr(pc.spool, "append", full)
    with httpx.Client(transport=transport) as connection:
        with pytest.raises(OSError):
            pc.receive_once(connection)
    assert not receipts
    assert robot.spool.oldest() == [original]


def test_lost_robot_ack_response_retries_without_duplicate_or_loss(tmp_path):
    robot, original, transport, _ = robot_fixture(tmp_path)
    pc = runtime_for(tmp_path / "pc")
    lost = [False]

    def route(request):
        if request.method == "POST" and not lost[0]:
            lost[0] = True
            return httpx.Response(503)
        return transport.handle_request(request)

    with httpx.Client(transport=httpx.MockTransport(route)) as connection:
        with pytest.raises(httpx.HTTPStatusError):
            pc.receive_once(connection)
        assert pc.spool.oldest() == [original]
        assert robot.spool.oldest() == [original]
        pc.receive_once(connection)
    assert pc.spool.oldest() == [original]
    assert robot.spool.status()["pending_windows"] == 0


def test_central_outage_keeps_local_preview_and_robot_link_loss_hides_values(tmp_path, monkeypatch):
    import time

    from rp1_monitor import relay

    _, _, transport, _ = robot_fixture(tmp_path)
    pc = runtime_for(tmp_path / "pc")
    with httpx.Client(transport=transport) as connection:
        pc.receive_once(connection)
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(503))) as connection:
        with pytest.raises(httpx.HTTPStatusError):
            pc.upload_once(connection)
    assert pc.snapshot().health["battery"]["soc"] == 80
    assert pc.snapshot().upload["collector_connected"] is True
    later = time.monotonic() + 6
    monkeypatch.setattr(relay.time, "monotonic", lambda: later)
    stale = pc.snapshot()
    assert stale.upload["collector_connected"] is False
    assert all(value is None for value in stale.values.values())
    assert stale.health["battery"]["soc"] is None
    assert all(row.age_ms >= 6000 for row in stale.processes)
    assert all(
        row["state"] == "unknown" and row["rss_mib"] is None
        for row in stale.health["processes"]["items"]
    )
    assert pc.spool.status()["pending_windows"] == 1


def test_relay_rejects_other_robot_or_simulated_source(tmp_path):
    robot, original, transport, receipts = robot_fixture(tmp_path)
    pc = runtime_for(tmp_path / "pc")
    pc.settings.source_kind = "ros2"
    with httpx.Client(transport=transport) as connection:
        with pytest.raises(ValueError, match="绑定"):
            pc.receive_once(connection)
    assert not receipts and robot.spool.oldest() == [original]
    assert pc.spool.status()["pending_windows"] == 0


def test_relay_doctor_labels_computer_environment_and_keeps_robot_profile(tmp_path):
    from fastapi.testclient import TestClient

    from rp1_monitor.agent import create_preview_app

    _, _, transport, _ = robot_fixture(tmp_path)
    pc = runtime_for(tmp_path / "pc")
    with httpx.Client(transport=transport) as connection:
        pc.receive_once(connection)
    api = TestClient(create_preview_app(pc, "test relay"))
    doctor = api.get("/api/v1/doctor")
    assert doctor.status_code == 200
    assert "上位机" in doctor.json()["environment"]["scope"]
    assert doctor.json()["environment"]["ros_observer_active"] is False
    assert doctor.json()["profile"]["robot_id"] == "RP1-TEST"
    pc._received_at -= 6
    stale = api.get("/api/v1/doctor").json()
    assert (
        next(item for item in stale["checks"] if item["key"] == "collector")["result"]
        == "attention"
    )
    assert stale["capabilities"][0]["state"] == "stale"
    assert (
        api.get("/api/v1/doctor", headers={"Origin": "https://untrusted.invalid"}).status_code
        == 403
    )


def test_upload_ack_and_network_failure_keep_unconfirmed_data(tmp_path, window):
    runtime = runtime_for(tmp_path)
    frame = window(stream_id=runtime.current_stream_id)
    runtime.current_sequence = frame.sequence
    runtime.spool.append(frame)

    def unavailable(request):
        return httpx.Response(503)

    with httpx.Client(transport=httpx.MockTransport(unavailable)) as client:
        with pytest.raises(httpx.HTTPStatusError):
            runtime.upload_once(client)
    assert runtime.spool.status()["pending_windows"] == 1

    def accept(request):
        assert request.headers["Authorization"] == "Bearer " + "a" * 32
        return httpx.Response(
            200,
            json=BatchAck(
                receipts=[{"stream_id": frame.stream_id, "sequence": frame.sequence}],
                inserted=1,
                duplicates=0,
            ).model_dump(mode="json"),
        )

    with httpx.Client(transport=httpx.MockTransport(accept)) as client:
        runtime.upload_once(client)
    assert runtime.spool.status()["pending_windows"] == 0


def test_latest_is_uploaded_before_backlog(tmp_path, window):
    runtime = runtime_for(tmp_path)
    runtime.spool.append(window(sequence=1, offset=-50))
    newest = window(sequence=2, stream_id=runtime.current_stream_id)
    runtime.current_sequence = 2
    runtime.spool.append(newest)
    seen = []

    def accept(request):
        import json

        payload = json.loads(request.content)
        seen.append(payload)
        return httpx.Response(
            200,
            json={
                "receipts": [
                    {"stream_id": value["stream_id"], "sequence": value["sequence"]}
                    for value in payload["windows"]
                ],
                "inserted": len(payload["windows"]),
                "duplicates": 0,
            },
        )

    with httpx.Client(transport=httpx.MockTransport(accept)) as client:
        runtime.upload_once(client)
        runtime.upload_once(client)
    assert seen[0]["delivery"] == "live" and seen[0]["windows"][0]["sequence"] == 2
    assert seen[1]["delivery"] == "backfill"


def test_late_arriving_backfill_does_not_hide_newest_source_window(tmp_path, window):
    import json

    runtime = runtime_for(tmp_path)
    runtime.current_sequence = 100
    latest = window(sequence=100, stream_id=runtime.current_stream_id)
    old = window(sequence=1, stream_id=runtime.current_stream_id, offset=-100)
    runtime.spool.append(latest)
    runtime.spool.append(old)
    seen = []

    def accept(request):
        payload = json.loads(request.content)
        seen.append(payload)
        return httpx.Response(
            200,
            json={
                "receipts": [{"stream_id": str(latest.stream_id), "sequence": 100}],
                "inserted": 1,
                "duplicates": 0,
            },
        )

    with httpx.Client(transport=httpx.MockTransport(accept)) as client:
        runtime.upload_once(client)
    assert seen[0]["delivery"] == "live"
    assert seen[0]["windows"][0]["sequence"] == 100
    assert runtime.spool.oldest() == [old]


def test_device_token_never_sent_over_lan_plaintext(tmp_path):
    with pytest.raises(ValueError, match="HTTPS"):
        runtime_for(tmp_path, "http://10.43.17.214:8443")


def test_slow_successful_uploads_do_not_starve_backlog(tmp_path, window):
    import json

    runtime = runtime_for(tmp_path)
    old = window(offset=-100)
    runtime.spool.append(old)
    runtime.current_sequence = 1
    runtime.spool.append(window(stream_id=runtime.current_stream_id))
    seen = []

    def new_window_during_request(request):
        payload = json.loads(request.content)
        seen.append(payload["delivery"])
        runtime.current_sequence += 1
        runtime.spool.append(
            window(stream_id=runtime.current_stream_id, sequence=runtime.current_sequence)
        )
        return httpx.Response(
            200,
            json={
                "receipts": [
                    {"stream_id": value["stream_id"], "sequence": value["sequence"]}
                    for value in payload["windows"]
                ],
                "inserted": len(payload["windows"]),
                "duplicates": 0,
            },
        )

    with httpx.Client(transport=httpx.MockTransport(new_window_during_request)) as client:
        for _ in range(8):
            runtime.upload_once(client)
    assert seen[:2] == ["live", "backfill"]
    assert all(item.stream_id != old.stream_id for item in runtime.spool.oldest())
    assert runtime.spool.status()["pending_windows"] <= 2


def test_rejected_backfill_is_retained_without_freezing_current_live(tmp_path, window):
    import json

    runtime = runtime_for(tmp_path)
    old = window(offset=-100)
    runtime.spool.append(old)
    live_sequences = []

    def accept_live_reject_old(request):
        payload = json.loads(request.content)
        if payload["delivery"] == "backfill":
            return httpx.Response(409, json={"detail": "historical conflict"})
        live_sequences.extend(item["sequence"] for item in payload["windows"])
        return httpx.Response(
            200,
            json={
                "receipts": [
                    {"stream_id": item["stream_id"], "sequence": item["sequence"]}
                    for item in payload["windows"]
                ],
                "inserted": 1,
                "duplicates": 0,
            },
        )

    with httpx.Client(transport=httpx.MockTransport(accept_live_reject_old)) as client:
        for sequence in range(1, 7):
            runtime.current_sequence = sequence
            runtime.spool.append(window(stream_id=runtime.current_stream_id, sequence=sequence))
            try:
                runtime.upload_once(client)
            except httpx.HTTPStatusError as error:
                assert error.response.status_code == 409
    assert live_sequences[-1] == 6
    assert runtime.backfill_error and runtime.spool.oldest()[0] == old
    assert runtime.spool.status()["pending_windows"] <= 2
