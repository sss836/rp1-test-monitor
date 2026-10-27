"""Per-robot human criteria must be versioned and never rewrite observations."""

import json

import pytest
from conftest import DEVICE_TOKEN, VIEWER_TOKEN
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import DBAPIError
from test_management import login, management_database, requires_postgres

from rp1_monitor.backend import create_app
from rp1_monitor.central_policy import CentralPolicy
from rp1_monitor.monitor_config import (
    MonitoringConfiguration,
    empty_configuration,
    monitoring_runtime,
)
from rp1_monitor.motor_map import MotorMapConfig
from rp1_monitor.storage import history, metadata


def configuration(dof=24, limit=60):
    value = empty_configuration(dof).model_dump()
    value["joints"][0].update(label="左膝电机", region="left_knee", bus="can0", motor_id="1")
    value["temperature_limits_c"] = {"joint.0.motor_temperature": limit}
    return value


@pytest.mark.parametrize("dof", [24, 28])
def test_configuration_complete_identity_and_no_default_safety_limits(dof):
    value = empty_configuration(dof)
    assert len(value.joints) == dof
    assert value.temperature_limits_c == {}
    assert not value.physical_mapping_verified
    assert all(item.joint_name is None and item.region is None for item in value.joints)
    assert "monitor_robot_config_versions" not in metadata.tables


@pytest.mark.parametrize(
    "change",
    [
        lambda value: value["joints"].pop(),
        lambda value: value["joints"][1].update(index=0),
        lambda value: value["joints"][0].update(index=24),
        lambda value: value["joints"][0].update(bus="can0", motor_id=None),
        lambda value: value["joints"][1].update(bus="can0", motor_id="1"),
        lambda value: [item.update(joint_name="same") for item in value["joints"][:2]],
        lambda value: value["joints"][0].update(label="  "),
        lambda value: value.update(scale_max_c=20),
        lambda value: value.update(scale_min_c=float("nan")),
        lambda value: value["temperature_limits_c"].update({"joint.24.motor_temperature": 60}),
        lambda value: value["temperature_limits_c"].update({"host.cpu_temperature": 60}),
        lambda value: value["temperature_limits_c"].update({"joint.0.position": 60}),
        lambda value: value["temperature_limits_c"].update(
            {"joint.0.motor_temperature": float("inf")}
        ),
    ],
)
def test_invalid_configuration_is_rejected(change):
    value = configuration()
    change(value)
    with pytest.raises(ValidationError):
        MonitoringConfiguration.model_validate(value)


def test_ros2_draft_must_confirm_actual_names_before_physical_mapping():
    value = empty_configuration()
    value.validate_source("ros2")
    value.physical_mapping_verified = True
    with pytest.raises(ValueError, match="实际关节名称"):
        value.validate_source("ros2")
    for item in value.joints:
        item.joint_name = f"actual_joint_{item.index}"
    assert value.validate_source("ros2") is value
    with pytest.raises(ValueError, match="身份或来源"):
        monitoring_runtime(
            {"source": "database", "robot_id": "OTHER", "source_kind": "ros2"},
            "RP1",
            "ros2",
            MotorMapConfig(),
            CentralPolicy(),
        )


def test_individual_motor_limits_preserve_global_nonmotor_criteria():
    fallback = CentralPolicy(
        temperature_limits_c={
            "battery.temperature": 55,
            "host.thermal.0.temperature": 80,
            "joint.0.motor_temperature": 70,
            "joint.1.mos_temperature": 75,
        }
    )
    envelope = {
        "robot_id": "RP1",
        "source_kind": "simulated",
        "source": "database",
        "configuration": configuration(limit=60),
        "revision": 4,
    }
    _, effective = monitoring_runtime(envelope, "RP1", "simulated", MotorMapConfig(), fallback)
    assert effective.temperature_limits_c == {
        "battery.temperature": 55,
        "host.thermal.0.temperature": 80,
        "joint.0.motor_temperature": 60,
    }
    assert fallback.temperature_limits_c["joint.0.motor_temperature"] == 70
    from rp1_monitor.monitor_alerts import conditions

    snapshot = {
        "online": True,
        "clock_unverified": False,
        "data_fresh": True,
        "age_seconds": 0,
        "quality": {
            "health_values": {"battery.temperature": 60, "joint.0.motor_temperature": 65},
            "health_ages_ms": {"battery.temperature": 0, "joint.0.motor_temperature": 0},
        },
    }
    rules = {item["rule_key"]: item for item in conditions(snapshot, None, effective)}
    assert rules["limit.battery.temperature"]["bad"] is True
    assert rules["limit.joint.0.motor_temperature"]["bad"] is True


def test_28_joint_paired_limits_plus_global_temperature_policy_can_be_reparsed():
    global_limits = {f"host.thermal.{index}.temperature": 80 for index in range(12)}
    fallback = CentralPolicy(temperature_limits_c=global_limits)
    value = configuration(28)
    value["temperature_limits_c"] = {
        f"joint.{index}.{sensor}_temperature": 65
        for index in range(28)
        for sensor in ("motor", "mos")
    }
    envelope = {
        "robot_id": "RP1",
        "source_kind": "simulated",
        "source": "database",
        "configuration": value,
        "revision": 3,
    }
    _, policy = monitoring_runtime(envelope, "RP1", "simulated", MotorMapConfig(), fallback)
    assert len(policy.temperature_limits_c) == 68
    assert CentralPolicy.model_validate(policy.model_dump()).digest == policy.digest


def test_old_database_configuration_read_is_device_bound_and_readonly(
    client, read_headers, write_headers
):
    envelope = client.get("/api/v1/robot-monitors/RP1-TEST/configuration", headers=read_headers)
    assert envelope.status_code == 200
    assert envelope.json()["source"] == "unconfigured"
    assert envelope.json()["revision"] is None
    assert (
        client.get("/api/v1/robot-monitors/UNKNOWN/configuration", headers=read_headers).status_code
        == 404
    )
    path = "/api/v1/robot-telemetry/configuration"
    assert client.get(path).status_code == 401
    assert client.get(path, headers=read_headers).status_code == 401
    assert client.get(path, headers=write_headers).json() == envelope.json()
    assert client.post(path, headers=write_headers, json={}).status_code == 405


@requires_postgres
def test_versioned_configuration_api_cas_roles_restart_and_independent_thermal_rules(
    tmp_path, window
):
    with management_database(tmp_path, configuration=True) as settings:
        settings.demo_read = False
        with TestClient(create_app(settings), base_url="http://127.0.0.1") as client:
            headers = login(client)
            store = client.app.state.store
            store.alert_monitor.stop()
            store.policy = CentralPolicy(alert_hold_seconds=0)
            path = "/api/v1/admin/robots/RP1-TEST/configuration"
            assert client.get(path).json()["revision"] is None
            body = {
                "expected_revision": None,
                "configuration": configuration(),
                "reason": "配置模拟阈值与机型",
            }
            assert client.post(path, json=body).status_code == 403
            first = client.post(path, headers=headers, json=body)
            assert first.status_code == 201, first.text
            revision = first.json()["revision"]
            assert client.post(path, headers=headers, json=body).status_code == 409
            create = {
                "robot_id": "OTHER-SIM",
                "name": "Different limit",
                "source_kind": "simulated",
                "reason": "不同模拟机阈值验证",
                "configuration": configuration(28, 90),
            }
            response = client.post("/api/v1/admin/robots", headers=headers, json=create)
            assert response.status_code == 201, response.text
            other_key = (tmp_path / "keys" / response.json()["credential_reference"]).read_text()
            assert response.json()["configuration_revision"] is not None
            for robot_id, token in (("RP1-TEST", DEVICE_TOKEN), ("OTHER-SIM", other_key)):
                sample = window(robot_id=robot_id)
                sample.quality.health_values = {"joint.0.motor_temperature": 80}
                sample.quality.health_ages_ms = {"joint.0.motor_temperature": 1}
                assert (
                    client.post(
                        "/api/v1/robot-telemetry/batches",
                        headers={"Authorization": f"Bearer {token}"},
                        json={"delivery": "live", "windows": [sample.model_dump(mode="json")]},
                    ).status_code
                    == 200
                )
            with store.engine.connect() as connection:
                before = connection.execute(
                    select(history.c.id, history.c.payload_hash, history.c.payload)
                ).all()
            snapshots = {item["robot_id"]: item for item in store.snapshots()["items"]}
            assert snapshots["RP1-TEST"]["health"]["motor_map"]["rows"][0]["state"] == "hot"
            assert snapshots["OTHER-SIM"]["health"]["motor_map"]["rows"][0]["state"] == "below"
            assert snapshots["OTHER-SIM"]["health"]["motor_map"]["dof"] == 28
            store.alert_monitor.tick()
            hot = next(
                item
                for item in store.alert_monitor.current("RP1-TEST")["items"]
                if item["rule_key"] == "limit.joint.0.motor_temperature"
            )
            cool = next(
                item
                for item in store.alert_monitor.current("OTHER-SIM")["items"]
                if item["rule_key"] == "limit.joint.0.motor_temperature"
            )
            assert (hot["state"], cool["state"]) == ("active", "normal")
            assert hot["policy_sha256"] != cool["policy_sha256"]
            assert hot["evidence"]["configuration_revision"] == revision
            assert (
                hot["evidence"]["facts"]["threshold_c"],
                cool["evidence"]["facts"]["threshold_c"],
            ) == (60, 90)
            body.update(
                expected_revision=revision,
                configuration=configuration(28, 95),
                reason="模拟策略降级验证",
            )
            updated = client.post(path, headers=headers, json=body)
            assert updated.status_code == 201, updated.text
            new_revision = updated.json()["revision"]
            assert new_revision > revision
            pending_evaluation = next(
                item
                for item in store.alert_monitor.current("RP1-TEST")["items"]
                if item["rule_key"] == "limit.joint.0.motor_temperature"
            )
            assert pending_evaluation["is_open"] is True
            assert pending_evaluation["evaluation_fresh"] is False
            assert "等待按当前配置" in pending_evaluation["evaluation_note"]
            assert client.post(path, headers=headers, json=body).status_code == 409
            assert (
                client.get(
                    "/api/v1/robot-telemetry/configuration",
                    headers={"Authorization": f"Bearer {DEVICE_TOKEN}"},
                ).json()["revision"]
                == new_revision
            )
            versions = client.get(path + "/versions?limit=1").json()
            assert (
                versions["items"][0]["id"] == new_revision
                and versions["next_before_id"] == new_revision
            )
            older = client.get(path + f"/versions?before_id={new_revision}").json()
            assert older["items"][0]["id"] == revision
            assert all(row["actor_db_login"].endswith("_person") for row in older["items"])
            with store.engine.connect() as connection:
                assert (
                    connection.execute(
                        select(history.c.id, history.c.payload_hash, history.c.payload)
                    ).all()
                    == before
                )
            audit = client.get("/api/v1/admin/audit?robot_id=RP1-TEST").json()["items"]
            assert [row["action"] for row in audit] == [
                "configuration.updated",
                "configuration.updated",
            ]
            assert (
                client.get(
                    "/api/v1/robot-monitors/RP1-TEST/configuration",
                    headers={"Authorization": f"Bearer {VIEWER_TOKEN}"},
                ).json()["revision"]
                == new_revision
            )

            config = json.loads(settings.admin_config.read_text())
            personal = create_engine(config["users"][0]["database_url"])
            namespace = config["schema_name"]
            try:
                for statement in (
                    f"DELETE FROM {namespace}.monitor_robot_config_versions",
                    f"UPDATE {namespace}.monitor_robot_config_versions SET reason='rewrite'",
                    f"INSERT INTO {namespace}.monitor_robot_config_versions DEFAULT VALUES",
                    f"SET ROLE {namespace}_admin_executor",
                    f"CREATE TABLE {namespace}.unreviewed(id int)",
                ):
                    with pytest.raises(DBAPIError), personal.begin() as connection:
                        connection.execute(text(statement))
                # Direct SQL validates again; registration and first version roll back together.
                invalid = configuration()
                invalid["joints"] = []
                with pytest.raises(DBAPIError), personal.begin() as connection:
                    connection.execute(
                        text(
                            f"SELECT {namespace}.admin_create_robot_with_configuration(:id,:name,'simulated',:digest,:reason,CAST(:configuration AS jsonb))"
                        ),
                        {
                            "id": "ATOMIC-FAIL",
                            "name": "Not committed",
                            "digest": "f" * 64,
                            "reason": "失败事务验证",
                            "configuration": json.dumps(invalid),
                        },
                    )
                assert not any(
                    row["robot_id"] == "ATOMIC-FAIL"
                    for row in client.get("/api/v1/admin/robots").json()["items"]
                )
                with pytest.raises(DBAPIError), store.engine.begin() as connection:
                    connection.execute(
                        text(
                            f"SELECT {namespace}.admin_save_robot_configuration('RP1-TEST',NULL,'{{}}'::jsonb,'禁止API写配置')"
                        )
                    )
            finally:
                personal.dispose()
        with TestClient(create_app(settings), base_url="http://127.0.0.1") as restarted:
            login(restarted)
            restored = restarted.get(path).json()
            assert restored["revision"] == new_revision
            assert restored["configuration"]["dof"] == 28
            assert len(restarted.get(path + "/versions").json()["items"]) == 2


@requires_postgres
def test_direct_sql_configuration_validation_matches_store_and_rejects_old_snapshot_fork(tmp_path):
    with management_database(tmp_path, configuration=True) as settings:
        config = json.loads(settings.admin_config.read_text())
        personal = create_engine(config["users"][0]["database_url"])
        namespace = config["schema_name"]
        statement = text(
            f"SELECT {namespace}.admin_save_robot_configuration('RP1-TEST',:expected,CAST(:configuration AS jsonb),'直接SQL软件验证')"
        )
        with TestClient(create_app(settings), base_url="http://127.0.0.1") as client:
            try:
                # Boundary whitespace uses the same complete set as Python str.strip().
                invalid_values = []
                for field, values in (
                    (
                        "hardware_revision",
                        ["\t", "\v", "\u00a0", " " * 121 + "x", "\u2000revision"],
                    ),
                    ("label", ["\u00a0", "value\n", " " * 81 + "x"]),
                    ("joint_name", ["\u001cactual", "name\u3000", " " * 121 + "x"]),
                    ("bus", ["can0\t", " " * 65 + "x"]),
                    ("motor_id", ["\u0085value", " " * 65 + "x"]),
                ):
                    for value in values:
                        candidate = configuration()
                        if field == "hardware_revision":
                            candidate[field] = value
                        else:
                            candidate["joints"][0][field] = value
                        invalid_values.append(candidate)
                for candidate in invalid_values:
                    with pytest.raises(DBAPIError) as error, personal.begin() as connection:
                        connection.execute(
                            statement, {"expected": None, "configuration": json.dumps(candidate)}
                        )
                    assert error.value.orig.sqlstate == "22023"
                good = configuration()
                good["hardware_revision"] = "v1"
                with personal.begin() as connection:
                    first = connection.execute(
                        statement, {"expected": None, "configuration": json.dumps(good)}
                    ).scalar_one()
                envelope = client.app.state.store.effective_monitoring_configuration("RP1-TEST")
                assert envelope["revision"] == first and envelope["configuration"] == good
                assert len(client.app.state.store.snapshots()["items"]) == 1
                # Repeatable-read snapshots must not create a second branch with the same previous ID.
                with personal.connect().execution_options(
                    isolation_level="REPEATABLE READ"
                ) as stale:
                    stale.execute(
                        text(f"SELECT count(*) FROM {namespace}.monitor_robot_config_versions")
                    )
                    newer = configuration(28, 95)
                    with personal.begin() as current:
                        second = current.execute(
                            statement, {"expected": first, "configuration": json.dumps(newer)}
                        ).scalar_one()
                    with pytest.raises(DBAPIError) as error:
                        stale.execute(
                            statement, {"expected": first, "configuration": json.dumps(good)}
                        )
                    assert error.value.orig.sqlstate in {"40001", "23505"}
                    stale.rollback()
                assert (
                    client.app.state.store.effective_monitoring_configuration("RP1-TEST")[
                        "revision"
                    ]
                    == second
                )
                assert (
                    client.app.state.store.snapshots()["items"][0]["health"]["motor_map"]["dof"]
                    == 28
                )
            finally:
                personal.dispose()
