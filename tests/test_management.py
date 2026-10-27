import json
import os
import secrets
from contextlib import contextmanager

import pytest
from conftest import DEVICE_TOKEN, VIEWER_TOKEN, require_disposable_postgres
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, make_url, select

from rp1_monitor.admin import password_hash, password_matches
from rp1_monitor.backend import BackendSettings, create_app
from rp1_monitor.storage import Store, robots


def test_database_managed_bootstrap_never_undoes_human_changes():
    from sqlalchemy import update

    store = Store("sqlite:///:memory:")
    device = dict(robot_id="RP1-ONE", name="Bootstrap", source_kind="simulated", token=DEVICE_TOKEN)
    store.configure([device], authoritative=False)
    with store.engine.begin() as connection:
        connection.execute(update(robots).values(name="Corrected", enabled=False))
    store.configure([device], authoritative=False)
    store.configure([], authoritative=False)
    with store.engine.connect() as connection:
        row = connection.execute(select(robots)).mappings().one()
        assert (row["name"], row["enabled"]) == ("Corrected", False)
    with pytest.raises(ValueError, match="不能混用"):
        store.configure([{**device, "source_kind": "ros2"}], authoritative=False)
    store.engine.dispose()


def test_admin_password_is_salted_and_constant_contract():
    first = password_hash("temporary-software-password")
    second = password_hash("temporary-software-password")
    assert first != second
    assert password_matches("temporary-software-password", first)
    assert not password_matches("wrong-password", first)


def test_mount_probe_requires_real_binding_and_lost_mount_is_unknown(tmp_path):
    from rp1_monitor.storage_mounts import StorageMounts

    config = tmp_path / "mounts.json"
    data = {
        "database_authority": "127.0.0.1:55433/monitor",
        "mounts": {
            name: {"path": str(tmp_path), "device_id": tmp_path.stat().st_dev, "reserve_bytes": 0}
            for name in ("database", "archives")
        },
    }
    config.write_text(json.dumps(data))
    probe = StorageMounts.load(config, make_url("postgresql+psycopg://127.0.0.1:55433/monitor"))
    assert probe.observe()["state"] == "measured"
    assert probe.observe()["free_bytes"] > 0
    probe.mounts["database"].device_id += 1
    assert probe.observe()["state"] == "unknown"
    assert probe.observe()["free_bytes"] is None
    with pytest.raises(ValueError, match="本机"):
        StorageMounts.load(config, make_url("postgresql+psycopg://remote.invalid:55433/monitor"))
    with pytest.raises(ValueError, match="不匹配"):
        StorageMounts.load(config, make_url("postgresql+psycopg://127.0.0.1:55433/other"))


@contextmanager
def management_database(tmp_path, *, configuration=False):
    import re
    from pathlib import Path

    from psycopg import sql

    url = make_url(require_disposable_postgres(os.environ["RP1_MONITOR_TEST_DATABASE_URL"]))
    namespace = "rp1_admin_web_" + secrets.token_hex(5)
    app_login, admin_login = namespace + "_runtime", namespace + "_person"
    app_password, admin_password = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    engine = create_engine(url)
    pooled = engine.raw_connection()
    connection = pooled.driver_connection
    roles = [
        namespace + suffix
        for suffix in ("_app", "_reader", "_archiver", "_admin", "_admin_executor")
    ]
    created = False
    try:
        migrations = (
            "001_schema.review.sql",
            "002_permissions.review.sql",
            "004_admin.review.sql",
        )
        if configuration:
            migrations += ("005_robot_configuration.review.sql",)
        for filename in migrations:
            source = (Path("deploy/postgres") / filename).read_text()
            source = re.sub(r"(?m)^(?:BEGIN;|COMMIT;)\s*$", "", source)
            connection.execute(source.replace("rp1_monitor", namespace), prepare=False)
        for login, password, group in (
            (app_login, app_password, namespace + "_app"),
            (admin_login, admin_password, namespace + "_admin"),
        ):
            connection.execute(
                sql.SQL(
                    "CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD {}"
                ).format(sql.Identifier(login), sql.Literal(password))
            )
            connection.execute(
                sql.SQL("GRANT {} TO {}").format(sql.Identifier(group), sql.Identifier(login))
            )
            connection.execute(
                sql.SQL("ALTER ROLE {} SET search_path={},pg_catalog").format(
                    sql.Identifier(login), sql.Identifier(namespace)
                )
            )
        connection.commit()
        created = True
        devices = tmp_path / "devices.json"
        devices.write_text(
            json.dumps(
                [
                    dict(
                        robot_id="RP1-TEST",
                        name="Bootstrap",
                        source_kind="simulated",
                        token=DEVICE_TOKEN,
                    )
                ]
            )
        )
        private = tmp_path / "admin.json"
        private.write_text(
            json.dumps(
                {
                    "schema_name": namespace,
                    "device_key_directory": str(tmp_path / "keys"),
                    "users": [
                        {
                            "username": "manager",
                            "password_hash": password_hash("temporary-software-password"),
                            "database_url": url.set(
                                username=admin_login, password=admin_password
                            ).render_as_string(hide_password=False),
                        }
                    ],
                }
            )
        )
        private.chmod(0o600)
        settings = BackendSettings(
            url.set(username=app_login, password=app_password).render_as_string(
                hide_password=False
            ),
            devices,
            VIEWER_TOKEN,
            initialize=False,
            admin_config=private,
        )
        yield settings
    finally:
        connection.rollback()
        if created:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(namespace)))
            for role in [app_login, admin_login, *roles]:
                connection.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))
            connection.commit()
        pooled.close()
        engine.dispose()


requires_postgres = pytest.mark.skipif(
    not os.environ.get("RP1_MONITOR_TEST_DATABASE_URL"),
    reason="需要隔离PostgreSQL验证管理API真实角色和持久性",
)


def login(client):
    response = client.post(
        "/api/v1/admin/login",
        json={"username": "manager", "password": "temporary-software-password"},
    )
    assert response.status_code == 200, response.text
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    return {"X-CSRF-Token": response.json()["csrf"]}


@requires_postgres
def test_management_api_real_roles_corrections_restart_and_no_key_exposure(tmp_path, window):
    with management_database(tmp_path) as settings:
        with TestClient(create_app(settings), base_url="http://127.0.0.1") as client:
            for headers in (
                {},
                {"Authorization": f"Bearer {DEVICE_TOKEN}"},
                {"Authorization": f"Bearer {VIEWER_TOKEN}"},
            ):
                assert client.get("/api/v1/admin/robots", headers=headers).status_code == 401
            headers = login(client)
            assert client.get("/admin").status_code == 200
            assert 'href="./admin"' in client.get("/").text
            create = {
                "robot_id": "NEW-SIM",
                "name": "New simulator",
                "source_kind": "simulated",
                "reason": "软件管理验证",
            }
            assert client.post("/api/v1/admin/robots", json=create).status_code == 403
            response = client.post("/api/v1/admin/robots", headers=headers, json=create)
            assert response.status_code == 201, response.text
            result = response.json()
            key_path = tmp_path / "keys" / result["credential_reference"]
            key = key_path.read_text()
            assert not key_path.stat().st_mode & 0o077
            assert key not in response.text and "digest" not in response.text
            frame = window().model_copy(update={"robot_id": "NEW-SIM"})
            assert (
                client.post(
                    "/api/v1/robot-telemetry/batches",
                    headers={"Authorization": f"Bearer {key}"},
                    json={"delivery": "live", "windows": [frame.model_dump(mode="json")]},
                ).status_code
                == 200
            )
            updated = {
                "expected_name": "New simulator",
                "expected_enabled": True,
                "name": "Corrected device",
                "enabled": False,
                "reason": "更正名称，停用登记",
            }
            assert (
                client.patch(
                    "/api/v1/admin/robots/NEW-SIM", headers=headers, json=updated
                ).status_code
                == 200
            )
            assert (
                client.patch(
                    "/api/v1/admin/robots/NEW-SIM", headers=headers, json=updated
                ).status_code
                == 409
            )
            assert (
                client.post(
                    "/api/v1/robot-telemetry/batches",
                    headers={"Authorization": f"Bearer {key}"},
                    json={"delivery": "live", "windows": [frame.model_dump(mode="json")]},
                ).status_code
                == 401
            )
            query = "/api/v1/admin/robots/NEW-SIM/windows?metric=joint.0.position"
            response = client.get(query)
            assert response.status_code == 200, response.text
            point = response.json()["items"][0]
            metrics = client.get("/api/v1/admin/robots/NEW-SIM/metrics").json()["items"]
            assert any(
                item["key"] == "joint.0.position" and item["unit"] == "rad" for item in metrics
            )
            original = point["original"]
            corrected = {**original, "last": 3.0, "minimum": 1.0, "maximum": 4.0}
            path = f"/api/v1/admin/windows/{point['window_id']}/corrections"
            body = {
                "metric": "joint.0.position",
                "expected_revision": None,
                "operation": "replace",
                "replacement": corrected,
                "reason": "观测核对",
            }
            response = client.post(path, headers=headers, json=body)
            assert response.status_code == 201, response.text
            revision = response.json()["revision"]
            assert client.post(path, headers=headers, json=body).status_code == 409
            point = client.get(query).json()["items"][0]
            assert point["original"] == original and point["effective"] == corrected
            body.update(
                expected_revision=revision,
                operation="restore",
                replacement=None,
                reason="撤销人工更正",
            )
            assert client.post(path, headers=headers, json=body).status_code == 201
            assert client.get(query).json()["items"][0]["effective"] == original
            audit = client.get("/api/v1/admin/audit?robot_id=NEW-SIM").json()["items"]
            assert [row["action"] for row in audit] == [
                "metric.restore",
                "metric.replace",
                "robot.updated",
                "robot.created",
            ]
            assert all(row["actor_db_login"].endswith("_person") for row in audit)
            assert key not in json.dumps(audit) and "token_digest" not in json.dumps(audit)
            assert client.post("/api/v1/admin/logout", headers=headers, json={}).status_code == 200
            assert client.get("/api/v1/admin/robots").status_code == 401
        with TestClient(create_app(settings), base_url="http://127.0.0.1") as restarted:
            login(restarted)
            items = restarted.get("/api/v1/admin/robots").json()["items"]
            assert next(item for item in items if item["robot_id"] == "NEW-SIM") == {
                "robot_id": "NEW-SIM",
                "name": "Corrected device",
                "source_kind": "simulated",
                "enabled": False,
            }
            assert len(restarted.get("/api/v1/admin/audit?robot_id=NEW-SIM").json()["items"]) == 4


@requires_postgres
def test_management_login_csrf_origin_validation_and_attempt_limit(tmp_path):
    with management_database(tmp_path) as settings:
        settings.demo_read = True
        with TestClient(create_app(settings), base_url="http://127.0.0.1") as client:
            payload = {"username": "manager", "password": "temporary-software-password"}
            assert (
                client.post(
                    "/api/v1/admin/login",
                    headers={"Origin": "https://example.invalid"},
                    json=payload,
                ).status_code
                == 403
            )
            invalid = client.post(
                "/api/v1/admin/login", json={**payload, "password": "private-too-long-" * 100}
            )
            assert invalid.status_code == 422 and "private-too-long" not in invalid.text
            headers = login(client)
            create = {
                "robot_id": "FAKE-ROS",
                "name": "Rejected source",
                "source_kind": "ros2",
                "reason": "来源隔离验证",
            }
            assert (
                client.post("/api/v1/admin/robots", headers=headers, json=create).status_code == 422
            )
            assert (
                client.post(
                    "/api/v1/admin/logout",
                    headers={**headers, "Origin": "https://example.invalid"},
                    json={},
                ).status_code
                == 403
            )
            assert (
                client.post(
                    "/api/v1/admin/logout", headers={"X-CSRF-Token": "wrong"}, json={}
                ).status_code
                == 403
            )
            assert client.post("/api/v1/admin/logout", headers=headers, json={}).status_code == 200
            for _ in range(5):
                assert (
                    client.post(
                        "/api/v1/admin/login", json={**payload, "password": "wrong-password-value"}
                    ).status_code
                    == 401
                )
            assert client.post("/api/v1/admin/login", json=payload).status_code == 429


@requires_postgres
def test_management_rejects_database_identity_override_and_dangerous_membership(tmp_path):
    from psycopg import sql

    with management_database(tmp_path) as settings:
        data = json.loads(settings.admin_config.read_text())
        configured = make_url(data["users"][0]["database_url"])
        data["users"][0]["database_url"] = (
            configured.set(username="misleading_alias")
            .update_query_dict({"user": configured.username})
            .render_as_string(hide_password=False)
        )
        settings.admin_config.write_text(json.dumps(data))
        with TestClient(create_app(settings), base_url="http://127.0.0.1") as client:
            response = client.post(
                "/api/v1/admin/login",
                json={"username": "manager", "password": "temporary-software-password"},
            )
            assert response.status_code == 403
        data["users"][0]["database_url"] = configured.render_as_string(hide_password=False)
        settings.admin_config.write_text(json.dumps(data))
        owner = create_engine(
            require_disposable_postgres(os.environ["RP1_MONITOR_TEST_DATABASE_URL"])
        )
        pooled = owner.raw_connection()
        try:
            pooled.driver_connection.execute(
                sql.SQL("GRANT {} TO {}").format(
                    sql.Identifier(data["schema_name"] + "_app"),
                    sql.Identifier(configured.username),
                )
            )
            pooled.driver_connection.commit()
        finally:
            pooled.close()
            owner.dispose()
        with TestClient(create_app(settings), base_url="http://127.0.0.1") as client:
            response = client.post(
                "/api/v1/admin/login",
                json={"username": "manager", "password": "temporary-software-password"},
            )
            assert response.status_code == 403
