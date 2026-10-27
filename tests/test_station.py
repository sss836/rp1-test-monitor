"""Local password connection: origin/authentication, identity and no robot commands."""

from pathlib import Path
from types import SimpleNamespace

import httpx
import paramiko
import pytest
from fastapi.testclient import TestClient

from rp1_monitor.aggregation import Accumulator
from rp1_monitor.ssh_transport import HostKeyRequired, check_host_key, fingerprint, parse_ssh_target
from rp1_monitor.station import ConnectRequest, create_station_app, prepare_state


@pytest.mark.parametrize("text", ["ssh robo@192.168.137.250", "robo@192.168.137.250"])
def test_plain_ssh_command_is_parsed_without_a_shell(text):
    assert parse_ssh_target(text) == ["robo", "192.168.137.250"]


@pytest.mark.parametrize("text", ["ssh -oProxyCommand=evil robo@host", "robo@host; id", "robo@host command",
                                      "robo@@host", "ssh robo@-host", "$(id)@host", "ssh root@host -N"])
def test_arbitrary_ssh_commands_are_rejected(text):
    with pytest.raises(ValueError):
        parse_ssh_target(text)


def test_host_key_requires_explicit_matching_confirmation_and_never_replaces_known():
    keys = paramiko.HostKeys()
    first, second = paramiko.RSAKey.generate(1024), paramiko.RSAKey.generate(1024)
    with pytest.raises(HostKeyRequired) as error:
        check_host_key(keys, "robot", first, "")
    assert error.value.fingerprint == fingerprint(first) and not keys.lookup("robot")
    assert check_host_key(keys, "robot", first, fingerprint(first))
    assert not check_host_key(keys, "robot", first, "")
    with pytest.raises(ValueError, match="不同"):
        check_host_key(keys, "robot", second, fingerprint(second))
    assert keys.check("robot", first)


def test_station_refuses_existing_unknown_directory_and_exposed_credentials(tmp_path):
    unknown = tmp_path / "existing"
    unknown.mkdir(mode=0o700)
    (unknown / "monitor.sqlite").write_text("unrelated database")
    with pytest.raises(ValueError, match="接管"):
        prepare_state(unknown)
    directory = prepare_state(tmp_path / "new")
    (directory / "viewer-token.txt").chmod(0o644)
    with pytest.raises(ValueError, match="私有"):
        prepare_state(directory)


@pytest.fixture
def station(tmp_path):
    app = create_station_app(tmp_path / "station", 18980)
    with TestClient(app, base_url="http://127.0.0.1:18980") as client:
        yield app, client, tmp_path / "station"


def bootstrap(client):
    assert client.get("/api/v1/station").status_code == 401
    response = client.get("/connect")
    assert response.status_code == 200
    assert "httponly" in response.headers["set-cookie"].lower()
    return client.get("/api/v1/station").json()["control_token"]


def test_cookie_bootstrap_readonly_access_and_control_origin_are_separate(station):
    _, client, _ = station
    control = bootstrap(client)
    assert client.get("/api/v1/robot-monitors").status_code == 200
    assert client.post("/api/v1/station/disconnect").status_code == 403
    assert client.post("/api/v1/station/disconnect", headers={
        "Origin": "https://other.example", "X-RP1-Station-Token": control}).status_code == 403
    assert client.get("/api/v1/station", headers={"Host": "evil.example"}).status_code == 403
    assert client.post("/api/v1/station/disconnect", headers={
        "Origin": "http://127.0.0.1:18980", "X-RP1-Station-Token": control}).status_code == 200


def test_invalid_request_never_echoes_password(station):
    _, client, _ = station
    control = bootstrap(client)
    response = client.post("/api/v1/station/connect", json={
        "target": "robo@host", "password": "PRIVATE_TEST_PASSWORD", "extra": "PRIVATE_TEST_PASSWORD"},
        headers={"X-RP1-Station-Token": control})
    assert response.status_code == 422 and "PRIVATE_TEST_PASSWORD" not in response.text


def test_host_challenge_is_readonly_and_no_password_is_saved(station):
    app, client, directory = station
    control = bootstrap(client)

    def challenge(*args, **kwargs):
        raise HostKeyRequired("robot", "SHA256:test-fingerprint")

    app.state.station.tunnel_factory = challenge
    response = client.post("/api/v1/station/connect", json={
        "target": "ssh robo@robot", "password": "PRIVATE_TEST_PASSWORD"},
        headers={"X-RP1-Station-Token": control})
    assert response.json()["status"] == "host_key_required"
    assert app.state.store.snapshots()["items"] == []
    for path in directory.iterdir():
        if path.is_file():
            assert b"PRIVATE_TEST_PASSWORD" not in path.read_bytes()


def test_robot_identity_and_source_register_only_after_valid_collector(station, monkeypatch):
    app, _, directory = station
    manager = app.state.station
    closed = []
    tunnel = SimpleNamespace(url="http://127.0.0.1:12345", transport=SimpleNamespace(is_active=lambda: True),
                             close=lambda: closed.append(True))
    manager.tunnel_factory = lambda *a, **k: tunnel
    frame = Accumulator("SIMULATED-SSH", "simulated").preview().model_dump(mode="json")
    frame["upload"] = {"stage": "robot_buffer"}
    seen = []
    original = httpx.Client

    def handler(request):
        if request.url.port != 12345:
            return httpx.Response(404)
        seen.append((request.method, request.url.path))
        return httpx.Response(200, json={"snapshot": frame})

    monkeypatch.setattr("rp1_monitor.station.httpx.Client", lambda **kwargs: original(
        transport=httpx.MockTransport(handler), **kwargs))
    monkeypatch.setattr(manager, "_start_relay", lambda device: setattr(manager, "relay_url", "http://127.0.0.1:12346"))
    result = manager.connect(ConnectRequest(target="robo@robot", password="PRIVATE_TEST_PASSWORD"))
    assert result["robot"] == {"robot_id": "SIMULATED-SSH", "source_kind": "simulated"}
    assert seen == [("GET", "/api/v1/doctor")]
    assert app.state.store.snapshots()["items"][0]["source_kind"] == "simulated"
    assert "PRIVATE_TEST_PASSWORD" not in (directory / "devices.json").read_text()
    frame["upload"] = {"stage": "upper_computer"}
    with pytest.raises(ValueError, match="正确采集端"):
        manager.connect(ConnectRequest(target="robo@robot", password="PRIVATE_TEST_PASSWORD"))
    assert closed


def test_connect_form_has_accessible_fields_and_does_not_store_secrets():
    web = Path(__file__).parents[1] / "src/rp1_monitor/web"
    html, js = (web / "connect.html").read_text(), (web / "connect.js").read_text()
    for field in ["ssh-target", "ssh-password", "ssh-port", "collector-port"]:
        assert f'for="{field}"' in html
    assert 'type="password"' in html and 'autocomplete="current-password"' in html
    assert "localStorage" not in js and "sessionStorage" not in js
    assert "textContent" in js and "innerHTML" not in js
    assert "simulated" in js and "模拟数据" in js


def test_existing_receiver_prevents_second_cache_ack_consumer(station, monkeypatch):
    app, _, directory = station
    manager = app.state.station
    closed = []
    manager.tunnel_factory = lambda *a, **k: SimpleNamespace(
        url="http://127.0.0.1:12345", close=lambda: closed.append(True))
    frame = Accumulator("SIMULATED-DUPLICATE", "simulated").preview().model_dump(mode="json")
    original = httpx.Client
    methods = []

    def handler(request):
        methods.append(request.method)
        return httpx.Response(200, json={"snapshot": {
            **frame, "upload": {"stage": "robot_buffer" if request.url.port == 12345 else "upper_computer"}
        }})

    monkeypatch.setattr("rp1_monitor.station.httpx.Client", lambda **kwargs: original(
        transport=httpx.MockTransport(handler), **kwargs))
    with pytest.raises(ValueError, match="已有"):
        manager.connect(ConnectRequest(target="robo@robot", password="test-only-password"))
    assert closed and set(methods) == {"GET"}
    assert (directory / "devices.json").read_text().strip() == "[]"
