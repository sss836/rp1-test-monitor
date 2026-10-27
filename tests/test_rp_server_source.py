import json

import httpx
import pytest

from rp1_monitor import aggregation
from rp1_monitor.aggregation import Accumulator
from rp1_monitor.rp_server_source import RpServerSource


class Recorder:
    def __init__(self):
        self.observations = []
        self.states = {}

    def observe(self, topic, values, publisher_stamp=None):
        self.observations.append((topic, values, publisher_stamp))

    def set_health_source(self, name, state, detail, **extra):
        self.states[name] = {"state": state, "detail": detail, **extra}


def snapshot(**changes):
    data = {
        "mock": False,
        "hw_ready": True,
        "hardware": {"bms": True, "motors": True},
        "battery": {
            "voltage": 51.8,
            "current": -2.35,
            "soc": 84.5,
            "temp": 32.1,
            "power_on": True,
            "protect": 65536,
            "soh": 0,
        },
        "motor_errors": [{"id": 42, "code": 2**31 + 5, "name": "原始驱动故障", "type": "DM"}],
    }
    data.update(changes)
    return data


def client_for(data, requests=None):
    def handle(request):
        if requests is not None:
            requests.append(request)
        return httpx.Response(200, json=data)

    return httpx.Client(transport=httpx.MockTransport(handle))


def test_only_get_status_and_preserves_real_battery_units_and_driver_fault_ids():
    recorder, requests = Recorder(), []
    source = RpServerSource(recorder, "http://127.0.0.1:9000/")
    with client_for(snapshot(), requests) as client:
        assert source.poll_once(client)
    assert len(requests) == 1
    assert requests[0].method == "GET" and requests[0].url.path == "/api/status"
    assert recorder.observations == [
        (
            "rp_server.status",
            {
                "battery.voltage": 51.8,
                "battery.current": -2.35,
                "battery.soc": 84.5,
                "battery.temperature": 32.1,
                "battery.power_enabled": 1.0,
                "battery.protection": 65536.0,
            },
            None,
        )
    ]
    reported = recorder.states["rp_server"]
    assert reported["reported_faults"] == [
        {"motor_id": 42, "code": 2**31 + 5, "name": "原始驱动故障"}
    ]
    assert reported["observed_at"].tzinfo is not None
    assert "缓存快照" in reported["detail"] and "不能判断硬件实时健康" in reported["detail"]
    assert not any(key.startswith("joint.") for key in recorder.observations[0][1])


@pytest.mark.parametrize("mock", [True, None, 0, "false"])
def test_mock_or_ambiguous_source_rejected(mock):
    recorder = Recorder()
    data = snapshot(mock=mock)
    if mock is None:
        del data["mock"]
    with client_for(data) as client:
        assert not RpServerSource(recorder, "http://localhost:9000").poll_once(client)
    assert recorder.observations == []
    assert recorder.states["bms"]["state"] == "unsupported"


@pytest.mark.parametrize(
    "changes",
    [
        {"hardware": {"bms": False, "motors": True}},
        {"hardware": {"bms": 1}},
        {"hardware": {}},
        {"battery": None},
    ],
)
def test_unready_or_absent_bms_does_not_refresh_cached_metrics_but_keeps_reported_faults(changes):
    recorder = Recorder()
    with client_for(snapshot(**changes)) as client:
        assert RpServerSource(recorder, "http://localhost:9000").poll_once(client)
    assert recorder.observations == []
    assert recorder.states["bms"]["state"] == "waiting"
    assert recorder.states["rp_server"]["reported_faults"][0]["motor_id"] == 42


@pytest.mark.parametrize("voltage", [0, -1, float("nan"), float("inf"), True, 1001, 10**1000])
def test_invalid_voltage_suppresses_entire_battery_batch(voltage):
    recorder = Recorder()
    data = snapshot()
    data["battery"]["voltage"] = voltage
    # json.dumps intentionally supports NaN/Infinity to verify rejection of a nonstandard producer.
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=json.dumps(data)))
    ) as client:
        assert RpServerSource(recorder, "http://localhost:9000").poll_once(client)
    assert recorder.observations == []
    assert "整批" in recorder.states["bms"]["detail"]


def test_missing_fields_and_zero_temperature_are_unknown_without_zero_fill_or_soh():
    recorder = Recorder()
    data = snapshot()
    data["battery"] = {"voltage": 48.1, "temp": 0, "soh": 0}
    with client_for(data) as client:
        assert RpServerSource(recorder, "http://localhost:9000").poll_once(client)
    assert recorder.observations[0][1] == {"battery.voltage": 48.1}
    assert "温度零值有效性待确认" in recorder.states["bms"]["detail"]


def test_empty_errors_on_unready_hardware_does_not_claim_healthy():
    recorder = Recorder()
    with client_for(
        snapshot(hw_ready=False, hardware={"bms": True, "motors": False}, motor_errors=[])
    ) as client:
        assert RpServerSource(recorder, "http://localhost:9000").poll_once(client)
    state = recorder.states["rp_server"]
    assert state["reported_faults"] == []
    assert "不代表全电机健康" in state["detail"]
    assert "未全部满足" in state["detail"]


def test_missing_motor_errors_is_unknown_while_battery_can_be_observed():
    recorder = Recorder()
    data = snapshot()
    del data["motor_errors"]
    with client_for(data) as client:
        assert RpServerSource(recorder, "http://localhost:9000").poll_once(client)
    assert recorder.states["rp_server"]["state"] == "waiting"
    assert "故障状态未知" in recorder.states["rp_server"]["detail"]
    assert recorder.observations[0][1]["battery.soc"] == 84.5


@pytest.mark.parametrize("errors", [[{"id": 4, "code": -1}], [{"id": 4, "code": 2**32}], [None]])
def test_invalid_fault_codes_or_rows_are_not_truncated_or_relabelled(errors):
    recorder = Recorder()
    with client_for(snapshot(motor_errors=errors)) as client:
        assert not RpServerSource(recorder, "http://localhost:9000").poll_once(client)
    assert recorder.states["rp_server"]["state"] == "error"
    assert recorder.observations == []


def test_failed_poll_does_not_keep_old_values_current(monkeypatch):
    current = [1_000_000_000]
    monkeypatch.setattr(aggregation.time, "monotonic_ns", lambda: current[0])
    accumulator = Accumulator("HEALTH", "ros2")
    source = RpServerSource(accumulator, "http://localhost:9000")
    with client_for(snapshot()) as client:
        assert source.poll_once(client)
    assert accumulator.preview().values["battery.soc"] == 84.5
    current[0] += 6_000_000_000

    def failed(request):
        raise httpx.ReadTimeout("snapshot unavailable", request=request)

    with httpx.Client(transport=httpx.MockTransport(failed)) as client:
        assert not source.poll_once(client)
    assert accumulator.preview().values["battery.soc"] is None
    assert accumulator.preview().health_sources["rp_server"].state == "error"


@pytest.mark.parametrize("failure", ["invalid-json", "oversized", "redirect"])
def test_bad_or_oversized_responses_and_redirects_are_visible_and_never_followed(failure):
    recorder, requests = Recorder(), []

    def handle(request):
        requests.append(request)
        if failure == "invalid-json":
            return httpx.Response(200, content="not json")
        if failure == "oversized":
            return httpx.Response(200, content=b"x" * (1024 * 1024 + 1))
        return httpx.Response(302, headers={"location": "http://localhost:9000/other"})

    with httpx.Client(transport=httpx.MockTransport(handle), follow_redirects=True) as client:
        assert not RpServerSource(recorder, "http://localhost:9000").poll_once(client)
    assert len(requests) == 1 and requests[0].method == "GET"
    assert recorder.observations == []
    assert recorder.states["rp_server"]["state"] == "error"


@pytest.mark.parametrize(
    "url",
    [
        "ws://localhost:9000",
        "http://u:p@localhost",
        "http://localhost?q=secret",
        "http://localhost/api/control",
        "http://localhost:99999",
    ],
)
def test_url_cannot_embed_credentials_or_select_another_endpoint(url):
    with pytest.raises(ValueError):
        RpServerSource(Recorder(), url)
