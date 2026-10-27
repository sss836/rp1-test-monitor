"""Independent robot pages serve only a shell; API credentials remain mandatory."""

import json
from html.parser import HTMLParser
from urllib.parse import urljoin

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from rp1_monitor.backend import BackendSettings, create_app


class ShellLinks(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.base = []
        self.assets = []
        self.links = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "base":
            self.base.append(values.get("href"))
        if tag == "script" and values.get("src"):
            self.assets.append(values["src"])
        if tag == "a":
            self.links.append(values.get("href"))


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/robots",
        "/robots/SIM/overview",
        "/robots/SIM/temperature",
        "/robots/SIM/history",
        "/robots/SIM/processes",
        "/robots/SIM/storage",
        "/robots/SIM/diagnostics",
    ],
)
def test_robot_workspace_direct_links_return_nonsecret_shell(client, path):
    response = client.get(path)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["cache-control"] == "no-store"
    shell = ShellLinks(response.text)
    assert shell.base == ["/"]
    assert "./admin" not in shell.links
    assert shell.assets
    asset = urljoin(urljoin(str(response.url), shell.base[0]), shell.assets[0])
    assert "/assets/" in asset
    assert client.get(asset).status_code == 200
    assert client.get("/api/v1/robot-monitors").status_code == 401


def test_workspace_does_not_query_robot_existence_or_reflect_robot_input(client):
    response = client.get("/robots/UNREGISTERED-ROBOT/overview")
    assert response.status_code == 200
    assert "UNREGISTERED-ROBOT" not in response.text
    assert client.get("/robots/SIM/arbitrary-page").status_code == 404
    assert client.get("/robots/SIM/overview/extra").status_code == 404


@pytest.mark.parametrize("robot_id", ["%3Cscript%3E", "%22onclick%3D%22alert", "a%20b", "a" * 65])
def test_workspace_invalid_robot_ids_are_rejected_without_echo(client, robot_id):
    response = client.get(f"/robots/{robot_id}/overview")
    assert response.status_code == 422
    assert "<script>" not in response.text
    assert "onclick" not in response.text
    assert '"input"' not in response.text


def test_mounted_workspace_base_preserves_asset_and_api_prefix(client, read_headers):
    platform = FastAPI()
    platform.mount("/monitor", client.app)
    with TestClient(platform) as mounted:
        response = mounted.get("/monitor/robots/SIM/temperature", headers={"Host": "other.test"})
        assert response.status_code == 200
        shell = ShellLinks(response.text)
        assert shell.base == ["/monitor/"]
        base = urljoin(str(response.url), shell.base[0])
        asset = urljoin(base, shell.assets[0])
        assert "/monitor/assets/" in asset
        assert mounted.get(asset).status_code == 200
        api = urljoin(base, "./api/v1/robot-monitors")
        assert mounted.get(api).status_code == 401
        assert mounted.get(api, headers=read_headers).status_code == 200


def test_management_link_stays_relative_to_app_base(tmp_path, monkeypatch):
    from rp1_monitor import admin

    class ShellAdministration:
        def __init__(self, *_args):
            pass

        def router(self):
            return APIRouter()

        def close(self):
            pass

    monkeypatch.setattr(admin, "Administration", ShellAdministration)
    devices = tmp_path / "devices.json"
    devices.write_text(json.dumps([]))
    application = create_app(
        BackendSettings(
            "sqlite:///:memory:",
            devices,
            viewer_token="test-workspace-viewer-" + "v" * 32,
            admin_config=tmp_path / "administration.json",
        )
    )
    platform = FastAPI()
    platform.mount("/monitor", application)
    with TestClient(platform) as connection:
        response = connection.get("/monitor/robots/SIM/overview")
        shell = ShellLinks(response.text)
        assert shell.base == ["/monitor/"]
        assert "./admin" in shell.links
        assert urljoin(urljoin(str(response.url), shell.base[0]), "./admin").endswith(
            "/monitor/admin"
        )
