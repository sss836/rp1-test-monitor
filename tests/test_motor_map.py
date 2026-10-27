"""Temperature colors must preserve identity, freshness and configured limits."""

from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from rp1_monitor.central_policy import CentralPolicy
from rp1_monitor.health_view import build_health
from rp1_monitor.motor_map import MotorMapConfig, build_motor_map, temperature_color, text_color
from rp1_monitor.sources import simulated_motor_temperature
from rp1_monitor.storage import history


def config(robot_id="RP1-TEST", source="simulated", verified=False):
    return MotorMapConfig.model_validate(
        {
            "robots": {
                robot_id: {
                    "source_kind": source,
                    "physical_mapping_verified": verified,
                    "scale_min_c": 20,
                    "scale_max_c": 100,
                    "joints": [
                        {
                            "index": 0,
                            "joint_name": "left_knee",
                            "label": "左膝电机",
                            "region": "left_knee",
                        },
                        {
                            "index": 1,
                            "joint_name": "right_knee",
                            "label": "右膝电机",
                            "region": "right_knee",
                        },
                    ],
                }
            }
        }
    )


def health(temperature=80, mos=40, age=1, available=True):
    values = {
        "joint.0.motor_temperature": temperature,
        "joint.0.mos_temperature": mos,
        "joint.0.fault_code": 0,
        "joint.1.motor_temperature": 45,
        "joint.1.fault_code": 0,
    }
    return build_health(
        values,
        {key: age for key in values},
        {},
        {},
        ["left_knee", "right_knee"],
        available=available,
    )


LIMITS = {
    "joint.0.motor_temperature": 60,
    "joint.0.mos_temperature": 65,
    "joint.1.motor_temperature": 60,
}


def view(data=None, mapping=None, source="simulated", limits=None):
    return build_motor_map(
        "RP1-TEST",
        source,
        data or health(),
        mapping or config(),
        LIMITS if limits is None else limits,
    )


def test_hotspot_and_mos_use_individual_readings_and_shared_color():
    result = view()
    assert result["hot_count"] == 1
    left = next(r for r in result["regions"] if r["key"] == "left_knee")
    right = next(r for r in result["regions"] if r["key"] == "right_knee")
    assert left["state"] == "hot" and right["state"] == "below"
    assert left["color"] == temperature_color(80, 20, 100)
    row = result["rows"][0]
    assert row["readings"][0]["color"] != row["readings"][1]["color"]
    assert "80.0 °C / 限值 60.0 °C" in row["detail"]
    assert "超温" in row["detail"] and "模拟" in result["mapping_note"]
    mos = view(health(40, 90))
    assert mos["rows"][0]["value"] == 90 and mos["rows"][0]["state"] == "hot"


def test_boundary_and_cooling_follow_existing_central_policy_semantics():
    assert view(health(60))["rows"][0]["state"] == "below"
    assert view(health(60.01))["rows"][0]["state"] == "hot"
    assert view(health(45))["hot_count"] == 0
    assert view(health(45))["rows"][0]["color"] != view(health(90))["rows"][0]["color"]


@pytest.mark.parametrize(
    "data",
    [
        health(age=6000),
        health(available=False),
        build_health({}, {}, {}, {}, ["left_knee", "right_knee"]),
    ],
)
def test_unknown_never_reuses_previous_red_or_fills_zero(data):
    result = view(data)
    assert result["hot_count"] == 0
    assert all(r["state"] == "unknown" and r["color"] == "#c5ceda" for r in result["regions"])
    assert all(r["value"] is None for r in result["rows"])


def test_missing_mos_does_not_hide_known_overtemperature_but_blocks_all_clear():
    data = health()
    data["motors"]["items"][0]["mos_temperature"] = None
    assert view(data)["rows"][0]["state"] == "hot"
    data["motors"]["items"][0]["temperature"] = 40
    assert view(data)["rows"][0]["state"] == "unknown"
    assert view(data)["regions"][0]["color"] == "#c5ceda"


def test_no_invented_threshold_or_physical_mapping():
    assert view(limits={})["rows"][0]["state"] == "unconfigured"
    assert not view(mapping=config("OTHER"))["regions"]
    assert not view(source="ros2")["regions"]  # Simulated map cannot bind to ROS2.
    assert not view(source="ros2", mapping=config(source="ros2"))["regions"]
    mapped = config(source="ros2", verified=True)
    assert len(view(source="ros2", mapping=mapped)["regions"]) == 2
    data = health()
    data["motors"]["items"][0]["name"] = "different_joint"
    result = view(data, mapping=mapped, source="ros2")
    assert {r["key"] for r in result["regions"]} == {"right_knee"}
    assert "名称与映射不一致" in result["rows"][0]["detail"]


def test_palette_clamps_darkens_continuously_and_keeps_text_contrast():
    colors = [temperature_color(value, 20, 100) for value in range(20, 101)]
    assert len(set(colors)) == 81
    assert all(
        sum(bytes.fromhex(a[1:])) > sum(bytes.fromhex(b[1:])) for a, b in zip(colors, colors[1:])
    )
    assert temperature_color(-100, 20, 100) == colors[0]
    assert temperature_color(1000, 20, 100) == colors[-1]
    assert text_color(colors[0]) == "#142441" and text_color(colors[-1]) == "#ffffff"


def test_mapping_rejects_duplicate_indices_invalid_scales_and_guessed_ros_names():
    original = config().model_dump()
    for field, value in [("scale_max_c", 20), ("scale_min_c", float("nan"))]:
        raw = deepcopy(original)
        raw["robots"]["RP1-TEST"][field] = value
        with pytest.raises(ValidationError):
            MotorMapConfig.model_validate(raw)
    raw = deepcopy(original)
    raw["robots"]["RP1-TEST"]["joints"] *= 2
    with pytest.raises(ValidationError):
        MotorMapConfig.model_validate(raw)
    raw = deepcopy(original)
    raw["robots"]["RP1-TEST"]["source_kind"] = "ros2"
    raw["robots"]["RP1-TEST"]["joints"][0]["joint_name"] = None
    with pytest.raises(ValidationError):
        MotorMapConfig.model_validate(raw)


def test_demo_cycle_heats_then_cools_without_changing_default_simulation(tmp_path):
    import math

    from rp1_monitor.agent import AgentRuntime, AgentSettings

    assert simulated_motor_temperature(3, 0, True) == 35
    assert simulated_motor_temperature(3, 12 * math.pi, True) == 95
    assert simulated_motor_temperature(3, 24 * math.pi, True) == 35
    assert simulated_motor_temperature(2, 10, True) == simulated_motor_temperature(2, 10)
    with pytest.raises(ValueError, match="仅允许模拟"):
        AgentRuntime(AgentSettings("TEST", "ros2", state_dir=tmp_path, thermal_demo=True))


def test_dashboard_api_uses_policy_and_never_rewrites_raw_evidence(
    client, window, write_headers, read_headers
):
    store = client.app.state.store
    store.motor_map_config = config()
    store.policy = CentralPolicy(temperature_limits_c=LIMITS)
    sample = window()
    sample.quality.joint_names = ["left_knee", "right_knee"]
    sample.quality.health_values = {
        "joint.0.motor_temperature": 80,
        "joint.0.mos_temperature": 40,
        "joint.0.fault_code": 0,
        "joint.1.motor_temperature": 45,
        "joint.1.fault_code": 0,
    }
    sample.quality.health_ages_ms = {key: 1 for key in sample.quality.health_values}
    response = client.post(
        "/api/v1/robot-telemetry/batches",
        headers=write_headers,
        json={"delivery": "live", "windows": [sample.model_dump(mode="json")]},
    )
    assert response.status_code == 200
    with store.engine.connect() as conn:
        before = conn.execute(select(history.c.payload, history.c.payload_hash)).all()
    snapshot_health = client.get("/api/v1/robot-monitors", headers=read_headers).json()["items"][0][
        "health"
    ]
    assert snapshot_health["overall"]["state"] == "fault"
    assert snapshot_health["overall"]["label"] == "存在电机超温"
    result = snapshot_health["motor_map"]
    assert result["hot_count"] == 1 and len(result["regions"]) == 2
    store.motor_map_config = config("OTHER")
    result = client.get("/api/v1/robot-monitors", headers=read_headers).json()["items"][0][
        "health"
    ]["motor_map"]
    assert result["regions"] == []
    with store.engine.connect() as conn:
        assert conn.execute(select(history.c.payload, history.c.payload_hash)).all() == before


def test_example_files_only_bind_simulated_robots():
    root = Path(__file__).parents[1] / "examples"
    mapping = MotorMapConfig.load(root / "motor-map.simulated.json")
    assert all(
        m.source_kind == "simulated" and not m.physical_mapping_verified and len(m.joints) == m.dof
        for m in mapping.robots.values()
    )
    assert {m.dof for m in mapping.robots.values()} == {24, 28}
    assert (
        len(
            CentralPolicy.model_validate_json(
                (root / "thermal-policy.simulated.json").read_text()
            ).temperature_limits_c
        )
        == 56
    )


@pytest.mark.parametrize("count", [24, 28])
def test_both_dof_configurations_include_last_joint_and_missing_roster(count, tmp_path):
    from rp1_monitor.agent import AgentRuntime, AgentSettings

    runtime = AgentRuntime(
        AgentSettings("RP1-TEST", "simulated", state_dir=tmp_path, simulated_dof=count)
    )
    assert runtime.source.joints == count
    raw = config().model_dump()
    raw["robots"]["RP1-TEST"]["dof"] = count
    raw["robots"]["RP1-TEST"]["joints"] = [{"index": count - 1, "label": "末号电机，部位待确认"}]
    mapping = MotorMapConfig.model_validate(raw)
    key = f"joint.{count - 1}.motor_temperature"
    data = build_health({key: 90}, {key: 1}, {}, {}, [f"joint_{i}" for i in range(count)])
    result = build_motor_map("RP1-TEST", "simulated", data, mapping, {key: 60})
    assert len(result["rows"]) == count and result["dof"] == count
    assert result["rows"][-1]["state"] == "hot" and result["hot_count"] == 1
    assert result["rows"][-1]["region"] is None
    assert result["rows"][-1]["readings"][0]["value"] == 90
    assert "部位映射未配置" in result["rows"][-1]["detail"]
    empty = build_motor_map(
        "RP1-TEST", "simulated", build_health({}, {}, {}, {}), mapping, {key: 60}
    )
    assert len(empty["rows"]) == count and empty["rows"][-1]["state"] == "unknown"


def test_dof_mismatch_never_projects_a_different_robot_configuration():
    raw = config().model_dump()
    raw["robots"]["RP1-TEST"]["dof"] = 24
    mapping = MotorMapConfig.model_validate(raw)
    data = health()
    data["motors"]["items"].append(
        {"joint": 27, "name": "extra", "temperature": 90, "mos_temperature": None, "fault_code": 0}
    )
    result = view(data, mapping)
    assert result["regions"] == [] and len(result["rows"]) == 25
    assert "清单超出自由度配置" in result["mapping_note"]
    raw["robots"]["RP1-TEST"]["joints"].append(
        {"index": 27, "label": "invalid", "region": "left_knee"}
    )
    with pytest.raises(ValidationError, match="超出"):
        MotorMapConfig.model_validate(raw)


@pytest.mark.parametrize("count", [24, 28])
def test_central_api_does_not_truncate_last_joint_of_either_configuration(
    client, window, write_headers, read_headers, count
):
    raw = config().model_dump()
    raw["robots"]["RP1-TEST"]["dof"] = count
    store = client.app.state.store
    store.motor_map_config = MotorMapConfig.model_validate(raw)
    key = f"joint.{count - 1}.motor_temperature"
    store.policy = CentralPolicy(temperature_limits_c={key: 60})
    sample = window()
    sample.quality.joint_names = [f"joint_{i}" for i in range(count)]
    sample.quality.health_values = {key: 90}
    sample.quality.health_ages_ms = {key: 1}
    response = client.post(
        "/api/v1/robot-telemetry/batches",
        headers=write_headers,
        json={"delivery": "live", "windows": [sample.model_dump(mode="json")]},
    )
    assert response.status_code == 200
    result = client.get("/api/v1/robot-monitors", headers=read_headers).json()["items"][0][
        "health"
    ]["motor_map"]
    assert result["dof"] == count and len(result["rows"]) == count
    assert result["rows"][-1]["joint"] == count - 1 and result["rows"][-1]["state"] == "hot"
